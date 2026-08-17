"""
Workflow Manager - Core logic for vehicle enrollment workflow execution.

Provides methods:
- start(vin, brand): Start new enrollment
- current(vin): Get current state
- next(vin): Advance to next step in DAG
- retry(vin): Retry current step
- cancel(vin): Cancel enrollment
"""

import logging
from typing import Optional, Dict, Any
from django.db import transaction
from django.utils import timezone

from .models import (
    WorkflowDefinition,
    WorkflowStep,
    WorkflowSequence,
    VehicleEnrollmentState,
    EnrollmentHistory,
)
from .step_executor import NON_RETRYABLE_ERROR_CODES

logger = logging.getLogger(__name__)


class WorkflowManager:
    """
    Manages vehicle enrollment workflows across different OEM brands.
    Handles state transitions, step execution, and DAG traversal.
    """

    def __init__(self):
        self.step_executor = None  # Will be set when needed

    def _set_step_executor(self, executor):
        """Inject step executor dependency."""
        self.step_executor = executor

    def _log_history(
        self,
        enrollment: VehicleEnrollmentState,
        action: str,
        from_status: str = '',
        to_status: str = '',
        details: Dict = None
    ):
        """Log enrollment action to history."""
        EnrollmentHistory.objects.create(
            enrollment=enrollment,
            step_name=enrollment.current_step,
            from_status=from_status,
            to_status=to_status or enrollment.status,
            action=action,
            details=details or {}
        )

    def start(self, vin: str, brand: str) -> VehicleEnrollmentState:
        """
        Start a new enrollment for a vehicle.

        Args:
            vin: Vehicle identification number
            brand: OEM brand identifier

        Returns:
            VehicleEnrollmentState: The newly created enrollment state

        Raises:
            ValueError: If brand doesn't exist or enrollment already exists
        """
        logger.info(f"Starting enrollment for VIN={vin}, brand={brand}")

        # Check if enrollment already exists
        if VehicleEnrollmentState.objects.filter(vin=vin).exists():
            existing = VehicleEnrollmentState.objects.get(vin=vin)
            if existing.status in ['completed', 'cancelled']:
                raise ValueError(
                    f"Enrollment for VIN={vin} already exists with status={existing.status}"
                )
            return existing  # Return existing in-progress enrollment

        # Get workflow definition for brand
        try:
            workflow = WorkflowDefinition.objects.get(brand=brand, is_active=True)
        except WorkflowDefinition.DoesNotExist:
            raise ValueError(f"No active workflow found for brand={brand}")

        # Create enrollment state
        with transaction.atomic():
            enrollment = VehicleEnrollmentState.objects.create(
                vin=vin,
                brand=workflow,
                current_step='',
                status='pending',
                tries=0,
                step_data={}
            )

            self._log_history(
                enrollment,
                action='start',
                from_status='',
                to_status='pending',
                details={'brand': brand}
            )

        # Trigger next step
        return self.next(vin)

    def current(self, vin: str) -> Optional[VehicleEnrollmentState]:
        """
        Get current enrollment state for a VIN.

        Args:
            vin: Vehicle identification number

        Returns:
            VehicleEnrollmentState or None if not found
        """
        try:
            return VehicleEnrollmentState.objects.select_related('brand').get(vin=vin)
        except VehicleEnrollmentState.DoesNotExist:
            return None

    def next(self, vin: str) -> VehicleEnrollmentState:
        """
        Advance to the next step in the workflow DAG.

        For sync steps: executes immediately and follows DAG
        For async steps: executes and pauses DAG on acknowledgment

        Args:
            vin: Vehicle identification number

        Returns:
            VehicleEnrollmentState: Updated enrollment state

        Raises:
            ValueError: If enrollment not found or already terminal
        """
        logger.info(f"Processing next step for VIN={vin}")

        enrollment = self.current(vin)
        if not enrollment:
            raise ValueError(f"No enrollment found for VIN={vin}")

        if enrollment.status in ['completed', 'cancelled', 'failed']:
            logger.info(f"Enrollment for VIN={vin} is in terminal state: {enrollment.status}")
            return enrollment

        # Get first step if starting fresh
        if not enrollment.current_step:
            first_step = self._get_first_step(enrollment.brand)
            if not first_step:
                # No steps defined, mark as completed
                enrollment.mark_completed()
                self._log_history(enrollment, 'next', to_status='completed')
                return enrollment

            enrollment.current_step = first_step.name
            enrollment.mark_in_progress()
            enrollment.save()
            self._log_history(enrollment, 'next', to_status='in_progress', details={'step': first_step.name})
        else:
            # Move to next step from current
            next_step_name = self._get_next_step(enrollment.brand, enrollment.current_step)
            if not next_step_name:
                # No more steps, workflow complete
                enrollment.mark_completed()
                self._log_history(enrollment, 'next', to_status='completed')
                return enrollment

            # RACE CONDITION FIX: Re-check terminal status before advancing
            enrollment.refresh_from_db()
            if enrollment.status in ['completed', 'cancelled', 'failed']:
                logger.info(f"Cannot advance to next step for VIN={vin}: terminal status={enrollment.status}")
                return enrollment

            enrollment.current_step = next_step_name

            # RACE CONDITION FIX: Check status BEFORE mark_in_progress
            # This prevents cancelled enrollments from being overwritten
            latest = VehicleEnrollmentState.objects.filter(vin=vin).first()
            logger.info(f"next() for {vin}: checking DB status={latest.status if latest else 'None'}")
            if latest and latest.status in ['cancelled', 'failed', 'completed']:
                logger.info(f"Cannot advance for VIN={vin}: terminal status={latest.status} - BLOCKING")
                return enrollment

            enrollment.mark_in_progress()
            enrollment.save()

            self._log_history(enrollment, 'next', to_status='in_progress', details={'step': next_step_name})

        # Execute the current step (only execute, don't recurse)
        return self._execute_step_only(enrollment)

    def _execute_step_only(self, enrollment: VehicleEnrollmentState) -> VehicleEnrollmentState:
        """
        Execute the current step without recursing to next.
        Used after advancing to a new step.
        """
        step = WorkflowStep.objects.filter(
            workflow=enrollment.brand,
            name=enrollment.current_step
        ).first()

        if not step:
            logger.error(f"Step {enrollment.current_step} not found for brand {enrollment.brand.brand}")
            enrollment.mark_failed(f"Step {enrollment.current_step} not found")
            return enrollment

        logger.info(f"Executing step={step.name}, type={step.step_type} for VIN={enrollment.vin}")

        # RACE CONDITION FIX: Re-check terminal status before executing step
        enrollment.refresh_from_db()
        if enrollment.status in ['completed', 'cancelled', 'failed']:
            logger.info(f"Step execution aborted for VIN={enrollment.vin}: terminal status={enrollment.status}")
            return enrollment

        # Execute step
        if self.step_executor:
            result = self.step_executor.execute(enrollment, step)

            if result.get('success'):
                # Update step data
                if 'data' in result:
                    enrollment.step_data[step.name] = result.get('data', {})

                if step.step_type == 'async':
                    # Async step - wait for response
                    enrollment.mark_awaiting_async()
                    enrollment.save()
                    return enrollment
                else:
                    # Sync step - save state then auto-continue to next step
                    enrollment.save()
                    return self._continue_sync_workflow(enrollment.vin)
            else:
                # Step failed - check if it's retryable
                enrollment.tries += 1
                error_message = result.get('error', 'Unknown error')
                error_code = result.get('error_code', '')
                retryable = result.get('retryable', True)  # Default to retryable

                # Store detailed error info in step_data
                enrollment.step_data[step.name] = {
                    'error': error_message,
                    'error_code': error_code,
                    'response': result
                }

                # Non-retryable errors (validation/business logic) immediately fail
                if not retryable:
                    logger.info(f"Non-retryable error for VIN={enrollment.vin}, error_code={error_code}")
                    enrollment.error_message = f"[{error_code}] {error_message}" if error_code else error_message
                    enrollment.mark_failed(enrollment.error_message)
                    self._log_history(
                        enrollment,
                        'step_failed',
                        to_status='failed',
                        details={
                            'error': enrollment.error_message,
                            'error_code': error_code,
                            'retryable': False,
                            'tries': enrollment.tries
                        }
                    )
                    return enrollment

                enrollment.error_message = error_message
                enrollment.save()

                if enrollment.tries >= enrollment.max_tries:
                    enrollment.mark_failed(enrollment.error_message)
                    self._log_history(
                        enrollment,
                        'step_failed',
                        to_status='failed',
                        details={'error': enrollment.error_message, 'tries': enrollment.tries}
                    )
                else:
                    self._log_history(
                        enrollment,
                        'step_retry_needed',
                        to_status='in_progress',
                        details={'error': enrollment.error_message, 'tries': enrollment.tries}
                    )

        return enrollment

    def _continue_sync_workflow(self, vin: str) -> VehicleEnrollmentState:
        """
        Continue the workflow by executing next sync step.
        This is called after a successful sync step completes.
        Stops when it reaches an async step (which requires external callback).
        """
        # Refresh enrollment from DB to get latest step_data
        enrollment = VehicleEnrollmentState.objects.select_related('brand').get(vin=vin)

        # ABORT if enrollment is in terminal state (race condition protection)
        if enrollment.status in ['completed', 'cancelled', 'failed']:
            logger.info(f"Workflow aborted for VIN={vin}: status={enrollment.status} (terminal)")
            return enrollment

        # Find next step
        next_step_name = self._get_next_step(enrollment.brand, enrollment.current_step)
        if not next_step_name:
            # No more steps, workflow complete
            enrollment.mark_completed()
            self._log_history(enrollment, 'workflow_complete', to_status='completed')
            return enrollment

        # Get the step object to check type
        next_step = WorkflowStep.objects.filter(
            workflow=enrollment.brand,
            name=next_step_name
        ).first()

        if not next_step:
            enrollment.mark_failed(f"Step {next_step_name} not found")
            return enrollment

        # Update enrollment to new step
        enrollment.current_step = next_step_name
        enrollment.mark_in_progress()
        enrollment.save()
        self._log_history(enrollment, 'next', to_status='in_progress', details={'step': next_step_name})

        # Execute the step (this will handle sync vs async)
        return self._execute_step_only(enrollment)

    def _get_first_step(self, workflow: WorkflowDefinition) -> Optional[WorkflowStep]:
        """Get the first step in the workflow."""
        return WorkflowStep.objects.filter(
            workflow=workflow,
            order=0
        ).first()

    def _get_next_step(self, workflow: WorkflowDefinition, current_step: str) -> Optional[str]:
        """
        Get the next step from the DAG based on current step.
        Uses sequence definitions to determine next step.
        """
        # Try to find a sequence with on_success condition
        sequences = WorkflowSequence.objects.filter(
            workflow=workflow,
            from_step=current_step,
            condition='on_success'
        ).order_by('order')

        if sequences.exists():
            return sequences.first().to_step

        # Fall back to any sequence with 'always' condition
        sequences = WorkflowSequence.objects.filter(
            workflow=workflow,
            from_step=current_step,
            condition='always'
        ).order_by('order')

        if sequences.exists():
            return sequences.first().to_step

        # No explicit transition, try to find step with next order
        current_step_obj = WorkflowStep.objects.filter(
            workflow=workflow,
            name=current_step
        ).first()

        if current_step_obj:
            next_step = WorkflowStep.objects.filter(
                workflow=workflow,
                order__gt=current_step_obj.order
            ).order_by('order').first()
            return next_step.name if next_step else None

        return None

    def retry(self, vin: str) -> VehicleEnrollmentState:
        """
        Retry the current step for an enrollment.

        Args:
            vin: Vehicle identification number

        Returns:
            VehicleEnrollmentState: Updated enrollment state

        Raises:
            ValueError: If enrollment not found or not in retryable state
        """
        logger.info(f"Retrying enrollment for VIN={vin}")

        enrollment = self.current(vin)
        if not enrollment:
            raise ValueError(f"No enrollment found for VIN={vin}")

        if enrollment.status not in ['in_progress', 'failed']:
            raise ValueError(
                f"Cannot retry enrollment in status={enrollment.status}"
            )

        # Clear error but DON'T reset tries - we track total attempts
        enrollment.error_message = ''
        enrollment.status = 'in_progress'
        enrollment.save()

        self._log_history(enrollment, 'retry', to_status='in_progress', details={'current_tries': enrollment.tries})

        # Re-execute current step
        return self._execute_step_only(enrollment)

    def cancel(self, vin: str) -> VehicleEnrollmentState:
        """
        Cancel an enrollment.

        Args:
            vin: Vehicle identification number

        Returns:
            VehicleEnrollmentState: Updated enrollment state

        Raises:
            ValueError: If enrollment not found or already terminal
        """
        logger.info(f"Cancelling enrollment for VIN={vin}")

        enrollment = self.current(vin)
        if not enrollment:
            raise ValueError(f"No enrollment found for VIN={vin}")

        if enrollment.status in ['completed', 'cancelled']:
            return enrollment

        old_status = enrollment.status
        enrollment.mark_cancelled()

        self._log_history(enrollment, 'cancel', from_status=old_status, to_status='cancelled')

        return enrollment

    def handle_async_complete(
        self,
        vin: str,
        request_id: str,
        success: bool,
        response_data: Dict = None,
        error_message: str = '',
        product_id: str = None
    ) -> VehicleEnrollmentState:
        """
        Handle async completion for an enrollment.
        Called by async handler when OEM returns success/failure.

        Handles multi-product completion (one message per product):
        - Accumulates completed products
        - Only continues to next step when ALL products complete
        - Handles mixed success/failure per product

        Args:
            vin: Vehicle identification number
            request_id: OEM's request ID for the async call
            success: Whether this specific message indicates success
            response_data: Response data from OEM
            error_message: Error message if failed
            product_id: Optional product identifier (for multi-product enrollment)

        Returns:
            VehicleEnrollmentState: Updated enrollment state
        """
        from .models import AsyncEnrollmentRequest

        logger.info(f"Handling async complete for VIN={vin}, request_id={request_id}, "
                    f"product={product_id}, success={success}")

        enrollment = self.current(vin)
        if not enrollment:
            raise ValueError(f"No enrollment found for VIN={vin}")

        # RACE CONDITION FIX: Re-fetch from DB to get latest status
        enrollment.refresh_from_db()
        logger.info(f"handle_async_complete for {vin}: status={enrollment.status}")

        if enrollment.status == 'cancelled':
            logger.info(f"handle_async_complete BLOCKED for {vin}: cancelled")
            return enrollment

        if enrollment.status != 'awaiting_async':
            logger.info(f"Enrollment not awaiting async for VIN={vin}, status={enrollment.status}")
            return enrollment

        # Find the async request
        async_request = AsyncEnrollmentRequest.objects.filter(
            enrollment=enrollment,
            request_id=str(request_id)
        ).first()

        if not async_request:
            logger.warning(f"Async request not found: {request_id}")
            # CRITICAL FIX: Re-fetch for terminal state check BEFORE setting to in_progress
            enrollment = self.current(vin)
            if enrollment.status in ['cancelled', 'failed']:
                logger.info(f"Aborting async completion for VIN={vin}: terminal status={enrollment.status}")
                return enrollment
            # Fall back to old behavior for backwards compatibility
            if success:
                enrollment.status = 'in_progress'
                enrollment.save()
                return self.next(vin)
            return enrollment

        if success:
            # Mark product as completed
            if product_id:
                all_done = async_request.mark_product_completed(product_id, response_data)
            else:
                # Single product mode - just mark request complete
                async_request.mark_all_completed()
                all_done = True

            # Store response data in enrollment step_data
            step_key = enrollment.current_step
            if step_key not in enrollment.step_data:
                enrollment.step_data[step_key] = {}
            if response_data:
                if 'product_responses' not in enrollment.step_data[step_key]:
                    enrollment.step_data[step_key]['product_responses'] = {}
                enrollment.step_data[step_key]['product_responses'][product_id] = response_data

            if all_done:
                # CRITICAL FIX: Check terminal status BEFORE setting to in_progress
                # Re-fetch to check for terminal state (race condition protection)
                enrollment = self.current(vin)
                if enrollment.status in ['cancelled', 'failed']:
                    logger.info(f"Async completion aborted for VIN={vin}: terminal status={enrollment.status}")
                    return enrollment

                # All products completed - continue to next step
                logger.info(f"All products completed for VIN={vin}, continuing workflow")
                self._log_history(
                    enrollment,
                    'async_complete',
                    to_status='in_progress',
                    details={'request_id': request_id, 'all_products': True}
                )
                enrollment.status = 'in_progress'
                enrollment.save()

                return self.next(vin)
            else:
                # Still waiting for more products
                logger.info(f"Waiting for more products: pending={async_request.pending_products}")
                enrollment.step_data[step_key]['pending_products'] = async_request.pending_products
                enrollment.step_data[step_key]['completed_products'] = async_request.completed_products
                enrollment.save()
                return enrollment
        else:
            # Async failed - check for non-retryable error codes
            error_code = ''
            if response_data:
                error_code = str(response_data.get('error_code', ''))

            # Non-retryable error codes immediately fail
            if error_code in NON_RETRYABLE_ERROR_CODES:
                async_request.mark_failed(f"[{error_code}] {error_message}")
                enrollment.error_message = f"[{error_code}] {error_message}"
                enrollment.mark_failed(enrollment.error_message)
                self._log_history(
                    enrollment,
                    'async_failed',
                    to_status='failed',
                    details={
                        'error': enrollment.error_message,
                        'error_code': error_code,
                        'retryable': False
                    }
                )
                return enrollment

            # Retryable error - allow retries
            async_request.mark_failed(error_message)
            enrollment.tries += 1
            enrollment.error_message = error_message
            enrollment.status = 'in_progress'
            enrollment.save()

            if enrollment.tries >= enrollment.max_tries:
                enrollment.mark_failed(enrollment.error_message)
                self._log_history(
                    enrollment,
                    'async_failed',
                    to_status='failed',
                    details={'error': error_message}
                )
            else:
                self._log_history(
                    enrollment,
                    'async_retry_needed',
                    to_status='in_progress',
                    details={'error': error_message}
                )
                # Re-execute current step
                return self._execute_step_only(enrollment)

        return enrollment

    def get_workflow_steps(self, brand: str) -> list:
        """
        Get all steps for a workflow.

        Args:
            brand: OEM brand identifier

        Returns:
            List of workflow steps
        """
        try:
            workflow = WorkflowDefinition.objects.get(brand=brand, is_active=True)
            return list(WorkflowStep.objects.filter(workflow=workflow).order_by('order'))
        except WorkflowDefinition.DoesNotExist:
            return []
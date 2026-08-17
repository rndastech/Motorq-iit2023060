"""
Mock Step Executor for testing without real OEM APIs.

This executor returns mock responses for all sync/async calls,
allowing you to test the workflow without actual OEM integration.
"""

import logging
from typing import Dict, Any
from django.conf import settings

from .models import VehicleEnrollmentState, WorkflowStep, AsyncEnrollmentRequest

logger = logging.getLogger(__name__)


class MockStepExecutor:
    """
    Mock step executor that returns predefined responses.
    Use this for testing without real OEM APIs.
    """

    # Mock responses for different steps
    MOCK_RESPONSES = {
        'validate_vin': {
            'success': True,
            'data': {
                'valid': True,
                'make': 'maruti',
                'model': 'swift',
                'year': 2024
            }
        },
        'check_capabilities': {
            'success': True,
            'data': {
                'capabilities': ['gps', 'obd', 'immobilizer'],
                'supported': True
            }
        },
        'user_verification': {
            'success': True,
            'data': {
                'verified': True,
                'user_id': 'USR-12345',
                'message': 'User verification completed'
            }
        },
        'submit_enrollment': {
            'success': True,
            'data': {
                'request_id': 'MOCK-REQ-001',
                'status': 'pending',
                'message': 'Enrollment request submitted'
            }
        },
        'register_ready': {
            'success': True,
            'data': {
                'registered': True,
                'status': 'active',
                'vehicle_id': 'VEH-12345'
            }
        },
        'default': {
            'success': True,
            'data': {'status': 'ok'}
        }
    }

    def __init__(self):
        self.request_count = 0

    def execute(self, enrollment: VehicleEnrollmentState, step: WorkflowStep) -> Dict[str, Any]:
        """
        Execute a mock step - returns predefined response.
        For async steps, creates an AsyncEnrollmentRequest record.
        """
        self.request_count += 1
        step_name = step.name

        logger.info(
            f"[MOCK] Executing step={step_name} for VIN={enrollment.vin} "
            f"(request #{self.request_count})"
        )

        # Get mock response for this step
        response = self.MOCK_RESPONSES.get(
            step_name,
            self.MOCK_RESPONSES['default']
        ).copy()

        # For async steps, create the async request record
        if step.step_type == 'async':
            # Create a request ID
            request_id = f"MOCK-{enrollment.vin}-{step_name}-{self.request_count}"

            AsyncEnrollmentRequest.objects.create(
                enrollment=enrollment,
                request_id=request_id,
                products=step.products or [],
                pending_products=step.products or [],  # Initialize pending to track all products
                status='pending',
                response_data=response.get('data', {})
            )

            response['data'] = {
                'request_id': request_id,
                'message': f'Mock async request submitted for {step_name}'
            }

        return response


def use_mock_executor():
    """
    Call this to replace the real StepExecutor with MockStepExecutor.

    Usage:
        from enrollment.mock_executor import use_mock_executor
        wm = WorkflowManager()
        use_mock_executor(wm)
    """
    def set_executor(manager):
        mock = MockStepExecutor()
        manager._set_step_executor(mock)
        logger.info("Mock executor enabled - real OEM APIs will not be called")

    return set_executor
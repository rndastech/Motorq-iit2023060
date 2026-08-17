"""
Database models for the Vehicle Enrollment Workflow Manager.

Tables:
1. WorkflowDefinition - Defines the workflow steps per OEM brand
2. WorkflowSequence - DAG edge definitions (step transitions)
3. VehicleEnrollmentState - Per-VIN enrollment state tracking
4. AsyncEnrollmentRequest - Tracks pending async enrollment requests
"""

from django.db import models
from django.utils import timezone


class WorkflowDefinition(models.Model):
    """
    Defines the workflow steps for an OEM brand.
    Each brand has a sequence of steps (sync or async).
    """
    brand = models.CharField(max_length=50, unique=True, db_index=True)
    name = models.CharField(max_length=100)  # Human-readable name
    description = models.TextField(blank=True, default='')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'workflow_definition'
        verbose_name = 'Workflow Definition'
        verbose_name_plural = 'Workflow Definitions'

    def __str__(self):
        return f"{self.brand} - {self.name}"


class WorkflowStep(models.Model):
    """
    Individual step within a workflow definition.
    """
    STEP_TYPES = [
        ('sync', 'Synchronous'),
        ('async', 'Asynchronous'),
    ]

    workflow = models.ForeignKey(
        WorkflowDefinition,
        on_delete=models.CASCADE,
        related_name='steps'
    )
    name = models.CharField(max_length=50)  # Step identifier
    step_type = models.CharField(max_length=10, choices=STEP_TYPES, default='sync')
    order = models.IntegerField(default=0)  # Execution order
    api_endpoint = models.CharField(max_length=255)  # OEM API endpoint
    capabilities = models.JSONField(default=list, blank=True)  # Required capabilities
    products = models.JSONField(default=list, blank=True)  # Products for enrollment
    timeout_seconds = models.IntegerField(default=30)
    retryable = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'workflow_step'
        unique_together = ['workflow', 'name']
        ordering = ['order']

    def __str__(self):
        return f"{self.workflow.brand}:{self.name} ({self.step_type})"


class WorkflowSequence(models.Model):
    """
    DAG edge definitions for step transitions.
    Defines which step follows which under what condition.
    """
    CONDITIONS = [
        ('always', 'Always'),
        ('on_success', 'On Success'),
        ('on_failure', 'On Failure'),
    ]

    workflow = models.ForeignKey(
        WorkflowDefinition,
        on_delete=models.CASCADE,
        related_name='sequences'
    )
    from_step = models.CharField(max_length=50)
    to_step = models.CharField(max_length=50)
    condition = models.CharField(max_length=20, choices=CONDITIONS, default='always')
    order = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'workflow_sequence'
        ordering = ['order']

    def __str__(self):
        return f"{self.from_step} -> {self.to_step} ({self.condition})"


class VehicleEnrollmentState(models.Model):
    """
    Tracks the enrollment state for each vehicle (VIN).
    """
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('in_progress', 'In Progress'),
        ('awaiting_async', 'Awaiting Async Response'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
        ('cancelled', 'Cancelled'),
    ]

    vin = models.CharField(max_length=50, primary_key=True, db_index=True)
    brand = models.ForeignKey(
        WorkflowDefinition,
        on_delete=models.PROTECT,
        related_name='enrollments'
    )
    current_step = models.CharField(max_length=50, blank=True, default='')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    tries = models.IntegerField(default=0)
    max_tries = models.IntegerField(default=3)
    step_data = models.JSONField(default=dict, blank=True)  # Step-specific data
    error_message = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'vehicle_enrollment_state'
        verbose_name = 'Vehicle Enrollment State'
        verbose_name_plural = 'Vehicle Enrollment States'

    def __str__(self):
        return f"{self.vin} ({self.brand.brand}) - {self.status}"

    def mark_in_progress(self):
        """Mark enrollment as in progress."""
        # RACE CONDITION FIX: Force check DB status - don't trust self.status
        import logging
        logger = logging.getLogger(__name__)

        # Always re-fetch from DB to get latest status
        latest = VehicleEnrollmentState.objects.filter(vin=self.vin).first()
        if latest and latest.status in ['completed', 'cancelled', 'failed']:
            logger.info(f"mark_in_progress BLOCKED for {self.vin}: DB status={latest.status}")
            self.status = latest.status
            self.error_message = ''
            return

        # Only update if status is non-terminal
        self.status = 'in_progress'
        self.error_message = ''
        self.save(update_fields=['status', 'error_message', 'updated_at'])
        logger.info(f"mark_in_progress SUCCESS for {self.vin}")

    def mark_awaiting_async(self):
        """Mark enrollment as waiting for async response."""
        # RACE CONDITION FIX: Force check DB status - don't trust self.status
        import logging
        logger = logging.getLogger(__name__)

        # Always re-fetch from DB to get latest status
        latest = VehicleEnrollmentState.objects.filter(vin=self.vin).first()
        if latest and latest.status in ['completed', 'cancelled', 'failed']:
            logger.info(f"mark_awaiting_async BLOCKED for {self.vin}: DB status={latest.status}")
            self.status = latest.status
            self.error_message = ''
            return

        # Only update if status is non-terminal
        self.status = 'awaiting_async'
        self.save(update_fields=['status', 'updated_at'])
        logger.info(f"mark_awaiting_async SUCCESS for {self.vin}")

    def mark_completed(self):
        """Mark enrollment as completed."""
        # RACE CONDITION FIX: Always check DB to get latest status
        import logging
        logger = logging.getLogger(__name__)
        latest = VehicleEnrollmentState.objects.filter(vin=self.vin).first()
        if latest and latest.status in ['completed', 'cancelled', 'failed']:
            logger.info(f"mark_completed SKIPPED for {self.vin}: terminal status={latest.status}")
            self.status = latest.status
            return
        self.status = 'completed'
        self.completed_at = timezone.now()
        self.save(update_fields=['status', 'completed_at', 'updated_at'])

    def mark_failed(self, error_message=''):
        """Mark enrollment as failed."""
        # RACE CONDITION FIX: Always check DB to get latest status
        import logging
        logger = logging.getLogger(__name__)
        latest = VehicleEnrollmentState.objects.filter(vin=self.vin).first()
        if latest and latest.status in ['completed', 'cancelled', 'failed']:
            logger.info(f"mark_failed SKIPPED for {self.vin}: terminal status={latest.status}")
            self.status = latest.status
            return
        self.status = 'failed'
        self.error_message = error_message
        self.save(update_fields=['status', 'error_message', 'updated_at'])

    def mark_cancelled(self):
        """Mark enrollment as cancelled."""
        # RACE CONDITION FIX: Use atomic raw SQL update to ensure cancel is permanent
        # This prevents concurrent start() or handle_async_complete from overwriting
        import logging
        from django.db import connection
        logger = logging.getLogger(__name__)

        # Force update with raw SQL - this is atomic and can't be overwritten
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE vehicle_enrollment_state
                SET status = 'cancelled',
                    completed_at = %s,
                    updated_at = %s
                WHERE vin = %s AND status != 'completed'
                """,
                [timezone.now(), timezone.now(), self.vin]
            )

        # Refresh to get the actual status
        self.refresh_from_db()
        logger.info(f"mark_cancelled for {self.vin}: status={self.status}")


class AsyncEnrollmentRequest(models.Model):
    """
    Tracks pending async enrollment requests.
    Supports multi-product completion (waits for all products before continuing).
    """
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('processing', 'Processing'),
        ('partial', 'Partial Success'),
        ('success', 'Success'),
        ('failed', 'Failed'),
    ]

    enrollment = models.ForeignKey(
        VehicleEnrollmentState,
        on_delete=models.CASCADE,
        related_name='async_requests'
    )
    request_id = models.CharField(max_length=100, db_index=True)  # OEM's request ID
    products = models.JSONField(default=list)                      # Expected products to enroll
    completed_products = models.JSONField(default=list)            # Products that have completed
    pending_products = models.JSONField(default=list)              # Products still waiting
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    response_data = models.JSONField(default=dict, blank=True)
    error_message = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    def mark_product_completed(self, product_id: str, response_data: dict = None):
        """
        Mark a single product as completed.
        Handles both "diagnostics" and "PROD-DIAGNOSTICS" formats.
        Returns True if ALL products are now completed.
        """
        if not product_id:
            return len(self.pending_products) == 0

        # Normalize: strip "PROD-" prefix if present and lowercase
        normalized_id = product_id.replace('PROD-', '').lower()

        # Build a set of already completed (normalized) product IDs for quick lookup
        completed_normalized = set()
        for cp in self.completed_products:
            completed_normalized.add(cp.replace('PROD-', '').lower())

        # Only add if not already completed (by normalized comparison)
        if normalized_id not in completed_normalized:
            self.completed_products = list(self.completed_products) + [product_id]
            completed_normalized.add(normalized_id)

        # Remove from pending - only remove if normalized form matches
        new_pending = []
        for p in self.pending_products:
            p_normalized = p.replace('PROD-', '').lower()
            # Keep if its normalized form doesn't match our normalized product
            if p_normalized != normalized_id:
                new_pending.append(p)
        self.pending_products = new_pending

        # Update response data (accumulate)
        if response_data:
            new_response_data = dict(self.response_data)
            if 'product_responses' not in new_response_data:
                new_response_data['product_responses'] = {}
            new_response_data['product_responses'][product_id] = response_data
            self.response_data = new_response_data

        # Save changes to database
        self.save()

        # Check if all products completed
        return len(self.pending_products) == 0 and len(self.completed_products) > 0

    def mark_all_completed(self):
        """Mark the entire request as completed."""
        self.status = 'success'
        self.pending_products = []
        from django.utils import timezone
        self.completed_at = timezone.now()
        self.save()

    def mark_failed(self, error_message: str = ''):
        """Mark the request as failed."""
        self.status = 'failed'
        self.error_message = error_message
        from django.utils import timezone
        self.completed_at = timezone.now()
        self.save()

    class Meta:
        db_table = 'async_enrollment_request'
        verbose_name = 'Async Enrollment Request'
        verbose_name_plural = 'Async Enrollment Requests'

    def __str__(self):
        return f"{self.enrollment.vin} - {self.request_id} ({self.status})"


class EnrollmentHistory(models.Model):
    """
    Audit log of all enrollment state changes.
    """
    enrollment = models.ForeignKey(
        VehicleEnrollmentState,
        on_delete=models.CASCADE,
        related_name='history'
    )
    step_name = models.CharField(max_length=50, blank=True)
    from_status = models.CharField(max_length=20)
    to_status = models.CharField(max_length=20)
    action = models.CharField(max_length=50)  # start, next, retry, cancel, async_complete
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'enrollment_history'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.enrollment.vin} - {self.action} at {self.created_at}"
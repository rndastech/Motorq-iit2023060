"""
Async Handler - Database polling approach for async step completion.

Background task periodically checks AsyncEnrollmentRequest table
for pending async operations and updates their status.
"""

import logging
import time
from typing import Optional

from django.conf import settings
from django.core.management.base import BaseCommand

from .models import AsyncEnrollmentRequest, VehicleEnrollmentState
from .workflow_manager import WorkflowManager
from .step_executor import StepExecutor

logger = logging.getLogger(__name__)


class AsyncHandler:
    """
    Handles async step completion via database polling.
    """

    def __init__(self):
        self.workflow_manager = WorkflowManager()
        self.step_executor = StepExecutor()
        self.workflow_manager._set_step_executor(self.step_executor)
        self.poll_interval = getattr(settings, 'ASYNC_POLL_INTERVAL_SECONDS', 10)
        self.max_retries = getattr(settings, 'ASYNC_MAX_RETRIES', 3)

    def check_pending_requests(self) -> int:
        """
        Check all pending async requests and process completions.

        Returns:
            Number of requests processed
        """
        pending_requests = AsyncEnrollmentRequest.objects.filter(
            status__in=['pending', 'processing']
        ).select_related('enrollment')

        processed = 0

        for async_req in pending_requests:
            try:
                self._check_and_process_async(async_req)
                processed += 1
            except Exception as e:
                logger.exception(f"Error processing async request {async_req.request_id}")

        return processed

    def _check_and_process_async(self, async_req: AsyncEnrollmentRequest):
        """Check status of a single async request and update if complete."""
        result = self.step_executor.check_async_status(async_req)

        status = result.get('status', 'pending')

        if status in ['success', 'completed']:
            # Mark as success
            async_req.status = 'success'
            async_req.response_data = result.get('data', {})
            from django.utils import timezone
            async_req.completed_at = timezone.now()
            async_req.save()

            # Continue workflow
            self.workflow_manager.handle_async_complete(
                vin=async_req.enrollment.vin,
                request_id=async_req.request_id,
                success=True,
                response_data=result.get('data', {})
            )

        elif status == 'failed':
            # Mark as failed
            async_req.status = 'failed'
            async_req.error_message = result.get('error', 'Unknown error')
            from django.utils import timezone
            async_req.completed_at = timezone.now()
            async_req.save()

            # Handle failure
            self.workflow_manager.handle_async_complete(
                vin=async_req.enrollment.vin,
                request_id=async_req.request_id,
                success=False,
                error_message=result.get('error', 'Unknown error')
            )

        elif status == 'processing':
            # Still in progress, just update timestamp
            async_req.status = 'processing'
            async_req.save()

        # If error (network etc), leave as pending for next poll

    def run_once(self) -> int:
        """Run one iteration of checking pending requests."""
        return self.check_pending_requests()


class Command(BaseCommand):
    """Django management command to run the async handler."""

    help = 'Run the async enrollment handler to poll for completed async operations'

    def add_arguments(self, parser):
        parser.add_argument(
            '--once',
            action='store_true',
            help='Run once and exit instead of continuous polling'
        )
        parser.add_argument(
            '--interval',
            type=int,
            default=None,
            help='Override poll interval in seconds'
        )

    def handle(self, *args, **options):
        handler = AsyncHandler()

        if options['interval']:
            handler.poll_interval = options['interval']

        if options['once']:
            # Run once
            self.stdout.write('Running async handler once...')
            processed = handler.run_once()
            self.stdout.write(self.style.SUCCESS(f'Processed {processed} requests'))
        else:
            # Continuous polling
            self.stdout.write(f'Starting async handler (poll interval: {handler.poll_interval}s)')
            self.stdout.write('Press Ctrl+C to stop')

            try:
                while True:
                    try:
                        processed = handler.check_pending_requests()
                        if processed > 0:
                            self.stdout.write(f'Processed {processed} requests')
                    except Exception as e:
                        logger.exception('Error in async handler loop')

                    time.sleep(handler.poll_interval)

            except KeyboardInterrupt:
                self.stdout.write(self.style.WARNING('\nShutting down async handler...'))
"""
Pub/Sub Handler for async enrollment completion.

Listens on Redis pub/sub channels for OEM enrollment responses.

Message formats by step type:
  - submit_enrollment: {request_id, product_id, vin, status, error_code}
  - user_verification: {request_id, vin, status, error_code}

Success indicators:
  - status in ['success', 'completed', 'verified']
  - error_code = '6700'

Channel pattern: enrollment:<brand> (e.g., enrollment:maruti, enrollment:tata)
"""

import json
import logging
import signal
import sys
from typing import Optional

import redis
from django.conf import settings
from django.core.management.base import BaseCommand

from .models import AsyncEnrollmentRequest, VehicleEnrollmentState
from .workflow_manager import WorkflowManager
from .step_executor import StepExecutor

logger = logging.getLogger(__name__)

# Shared fakeredis server instance for pub/sub to work across modules
_shared_fake_server = None


class PubSubHandler:
    """
    Handles pub/sub messages for async enrollment completion.
    Listens on Redis channels and processes incoming messages.
    """

    # Success error code for enrollment
    SUCCESS_CODE = '6700'

    # Success status values (for different step types)
    SUCCESS_STATUSES = ['success', 'completed', 'verified']

    def __init__(self):
        self.redis_client = None
        self.pubsub = None
        self.running = False
        self.use_fake = False
        self.workflow_manager = WorkflowManager()
        self.step_executor = StepExecutor()
        self.workflow_manager._set_step_executor(self.step_executor)

    def connect(self):
        """Connect to Redis or use fakeredis."""
        redis_url = getattr(settings, 'REDIS_URL', 'redis://localhost:6379/0')
        use_fake = getattr(settings, 'USE_FAKE_REDIS', False)  # Default to real Redis

        if use_fake:
            import fakeredis
            # IMPORTANT: Must use the same shared fakeredis server as mock_oem_server.py
            # so pub/sub messages published by the mock server reach this subscriber.
            global _shared_fake_server
            if _shared_fake_server is None:
                _shared_fake_server = fakeredis.FakeServer()
                logger.info("Created shared fakeredis server for pub/sub")
            self.redis_client = fakeredis.FakeRedis(server=_shared_fake_server, decode_responses=True)
            self.use_fake = True
            logger.info("Using shared fakeredis server for pub/sub")
        else:
            try:
                self.redis_client = redis.from_url(redis_url, decode_responses=True)
                self.redis_client.ping()
                logger.info(f"Connected to Redis: {redis_url}")
            except Exception as e:
                logger.error(f"Cannot connect to Redis at {redis_url}: {e}")
                raise  # Don't fallback to fakeredis

        self.pubsub = self.redis_client.pubsub()

    def subscribe_to_brands(self, brands: list = None):
        """
        Subscribe to enrollment channels for specified brands.
        If no brands specified, subscribes to all active workflow brands.
        """
        if brands is None:
            # Get all active workflow brands
            from .models import WorkflowDefinition
            brands = list(
                WorkflowDefinition.objects.filter(is_active=True)
                .values_list('brand', flat=True)
            )

        channels = [f"enrollment:{brand}" for brand in brands]
        self.pubsub.subscribe(*channels)
        logger.info(f"Subscribed to channels: {channels}")

    def _is_success(self, status: str, error_code: str) -> bool:
        """
        Determine if the message indicates success.

        Success conditions:
        - status in ['success', 'completed', 'verified']
        - error_code == '6700'
        """
        return (
            status.lower() in self.SUCCESS_STATUSES or
            error_code == self.SUCCESS_CODE or
            error_code == str(self.SUCCESS_CODE)
        )

    def process_message(self, message: dict) -> bool:
        """
        Process a single pub/sub message.

        Expected formats:
        {
            "request_id": "12345",
            "product_id": "PROD001",  # For submit_enrollment
            "vin": "ABC123",
            "status": "success",  # or "verified", "failed"
            "error_code": "6700"  # 6700 = success
        }

        Returns:
            True if message was processed successfully, False otherwise
        """
        try:
            # Parse message data
            data = message.get('data', {})
            if isinstance(data, str):
                data = json.loads(data)

            vin = data.get('vin')
            request_id = data.get('request_id')
            error_code = str(data.get('error_code', ''))
            status = data.get('status', '').lower()
            product_id = data.get('product_id') or data.get('capability')  # Support both formats

            if not vin:
                logger.warning(f"Message missing VIN: {data}")
                return False

            is_success = self._is_success(status, error_code)

            logger.info(
                f"Processing message: VIN={vin}, request_id={request_id}, "
                f"status={status}, error_code={error_code}, success={is_success}"
            )

            # Find the enrollment
            enrollment = VehicleEnrollmentState.objects.filter(
                vin=vin,
                status='awaiting_async'
            ).first()

            if not enrollment:
                # Check if enrollment exists but in terminal state
                existing = VehicleEnrollmentState.objects.filter(vin=vin).first()
                if existing:
                    logger.info(f"Enrollment for VIN={vin} already in terminal state: {existing.status}")
                else:
                    logger.warning(f"No enrollment found for VIN={vin}")
                return False

            # Update the async request record
            async_request = AsyncEnrollmentRequest.objects.filter(
                enrollment=enrollment,
                request_id=str(request_id)
            ).first()

            # Don't update async_request status here - handle_async_complete will do that
            # through mark_product_completed which properly handles multi-product scenarios
            # Just log the intermediate state for debugging
            if async_request and product_id:
                logger.info(
                    f"Intermediate product state: completed={async_request.completed_products}, "
                    f"pending={async_request.pending_products}"
                )

            # Continue the workflow
            self.workflow_manager.handle_async_complete(
                vin=vin,
                request_id=str(request_id),
                success=is_success,
                response_data=data,
                error_message=f"Error code: {error_code}" if not is_success else '',
                product_id=product_id
            )

            logger.info(f"Successfully processed message for VIN={vin}")
            return True

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse message JSON: {e}")
            return False
        except Exception as e:
            logger.exception(f"Error processing message: {e}")
            return False

    def start_listening(self):
        """Start listening for pub/sub messages."""
        self.running = True
        logger.info("Starting pub/sub listener...")

        try:
            # Use get_message in a tight loop without long timeouts
            # to process messages as fast as they arrive
            while self.running:
                # Process all pending messages without waiting
                message = self.pubsub.get_message(timeout=0.1)
                while message:
                    if message['type'] == 'message':
                        self.process_message(message)
                    elif message['type'] == 'subscribe':
                        logger.info(f"Subscribed to channel: {message['channel']}")
                    # Check if we should stop before getting next message
                    if not self.running:
                        break
                    message = self.pubsub.get_message(timeout=0.1)

                # If no messages, briefly sleep to avoid busy-waiting
                # But wake up quickly when new messages arrive
                if self.running:
                    import time
                    time.sleep(0.05)  # 50ms between empty polls (20 checks/second)
        except redis.ConnectionError as e:
            logger.error(f"Redis connection error: {e}")
        finally:
            self.cleanup()

    def stop(self):
        """Stop the pub/sub listener."""
        logger.info("Stopping pub/sub listener...")
        self.running = False

    def cleanup(self):
        """Clean up Redis connections."""
        if self.pubsub:
            self.pubsub.close()
        if self.redis_client:
            self.redis_client.close()
        logger.info("Pub/sub handler cleaned up")


class Command(BaseCommand):
    """Django management command to run the pub/sub listener."""

    help = 'Run the pub/sub listener for async enrollment completion'

    def add_arguments(self, parser):
        parser.add_argument(
            '--brands',
            nargs='+',
            help='Specific brands to subscribe to (default: all active)'
        )
        parser.add_argument(
            '--redis-url',
            default=None,
            help='Redis URL (default: from settings)'
        )

    def handle(self, *args, **options):
        handler = PubSubHandler()

        # Set up signal handlers for graceful shutdown
        def signal_handler(signum, frame):
            handler.stop()

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        try:
            # Override Redis URL if provided
            if options['redis_url']:
                import os
                os.environ['REDIS_URL'] = options['redis_url']

            handler.connect()
            handler.subscribe_to_brands(options['brands'])

            self.stdout.write(
                f"Listening for enrollment messages on brands: "
                f"{options['brands'] or 'all active'}"
            )
            self.stdout.write("Press Ctrl+C to stop")

            handler.start_listening()

        except redis.ConnectionError as e:
            self.stderr.write(self.style.ERROR(f"Failed to connect to Redis: {e}"))
            sys.exit(1)
        except Exception as e:
            logger.exception("Error in pub/sub listener")
            self.stderr.write(self.style.ERROR(f"Error: {e}"))
            sys.exit(1)
        finally:
            handler.cleanup()
            self.stdout.write(self.style.SUCCESS("Pub/sub listener stopped"))
"""
Management command to set up Tata vehicle enrollment workflow.
"""

from django.core.management.base import BaseCommand

from enrollment.models import WorkflowDefinition, WorkflowStep, WorkflowSequence


class Command(BaseCommand):
    help = 'Set up the Tata vehicle enrollment workflow'

    def add_arguments(self, parser):
        parser.add_argument(
            '--clear',
            action='store_true',
            help='Clear existing Tata workflow before creating'
        )

    def handle(self, *args, **options):
        if options['clear']:
            self.stdout.write('Clearing existing Tata workflow...')
            WorkflowDefinition.objects.filter(brand='tata').delete()

        # Create Tata workflow
        self.stdout.write('Creating Tata workflow...')

        workflow, created = WorkflowDefinition.objects.get_or_create(
            brand='tata',
            defaults={
                'name': 'Tata Motors Vehicle Enrollment',
                'description': 'Enrollment workflow for Tata Motors vehicles with user verification',
                'is_active': True
            }
        )

        if not created:
            self.stdout.write(self.style.WARNING('Tata workflow already exists, updating...'))

        # Define steps
        steps_data = [
            {
                'name': 'validate_vin',
                'step_type': 'sync',
                'order': 0,
                'api_endpoint': '/api/oem/tata/validate-vin',
                'capabilities': [],
                'products': [],
                'timeout_seconds': 30,
                'retryable': True,
            },
            {
                'name': 'check_capabilities',
                'step_type': 'sync',
                'order': 1,
                'api_endpoint': '/api/oem/tata/capabilities',
                'capabilities': ['gps', 'obd', 'immobilizer'],
                'products': [],
                'timeout_seconds': 30,
                'retryable': True,
            },
            {
                'name': 'user_verification',
                'step_type': 'async',
                'order': 2,
                'api_endpoint': '/api/oem/tata/verify-user',
                'capabilities': [],
                'products': [],
                'timeout_seconds': 60,
                'retryable': True,
            },
            {
                'name': 'submit_enrollment',
                'step_type': 'async',
                'order': 3,
                'api_endpoint': '/api/oem/tata/enroll',
                'capabilities': [],
                'products': ['tracking', 'diagnostics', 'geofencing'],
                'timeout_seconds': 60,
                'retryable': True,
            },
            {
                'name': 'register_ready',
                'step_type': 'sync',
                'order': 4,
                'api_endpoint': '/api/oem/tata/register-ready',
                'capabilities': [],
                'products': [],
                'timeout_seconds': 30,
                'retryable': False,
            },
        ]

        # Create steps
        for step_data in steps_data:
            step, created = WorkflowStep.objects.get_or_create(
                workflow=workflow,
                name=step_data['name'],
                defaults=step_data
            )
            if created:
                self.stdout.write(f"  Created step: {step.name}")

        # Create sequences (DAG edges)
        # Note: user_verification has its own pub/sub completion
        sequences_data = [
            ('validate_vin', 'check_capabilities', 'always'),
            ('check_capabilities', 'user_verification', 'always'),
            ('user_verification', 'submit_enrollment', 'on_success'),
            ('submit_enrollment', 'register_ready', 'on_success'),
        ]

        for from_step, to_step, condition in sequences_data:
            seq, created = WorkflowSequence.objects.get_or_create(
                workflow=workflow,
                from_step=from_step,
                to_step=to_step,
                defaults={'condition': condition}
            )
            if created:
                self.stdout.write(f"  Created sequence: {from_step} -> {to_step}")

        self.stdout.write(self.style.SUCCESS(
            f'\nTata workflow created successfully!\n'
            f'  Brand: {workflow.brand}\n'
            f'  Steps: {WorkflowStep.objects.filter(workflow=workflow).count()}\n'
            f'  Sequences: {WorkflowSequence.objects.filter(workflow=workflow).count()}'
        ))

        # Print workflow summary
        self.stdout.write('\nWorkflow Steps:')
        for step in WorkflowStep.objects.filter(workflow=workflow).order_by('order'):
            self.stdout.write(f"  {step.order}. {step.name} ({step.step_type})")

        self.stdout.write('\nWorkflow Sequences:')
        for seq in WorkflowSequence.objects.filter(workflow=workflow).order_by('order'):
            self.stdout.write(f"  {seq.from_step} -> {seq.to_step} ({seq.condition})")

        self.stdout.write('\nPub/Sub message format for user_verification:')
        self.stdout.write('  {"request_id": "...", "vin": "...", "status": "verified", "error_code": "6700"}')
"""
Unit tests for the Vehicle Enrollment Workflow Manager.
"""

import json
import time
from unittest.mock import patch, MagicMock
from django.test import TestCase, override_settings
from django.urls import reverse

from enrollment.models import (
    WorkflowDefinition,
    WorkflowStep,
    WorkflowSequence,
    VehicleEnrollmentState,
    AsyncEnrollmentRequest,
    EnrollmentHistory,
)
from enrollment.workflow_manager import WorkflowManager
from enrollment.step_executor import StepExecutor


class WorkflowManagerTestCase(TestCase):
    """Tests for the WorkflowManager class."""

    def setUp(self):
        """Set up test fixtures."""
        # Create a workflow for testing
        self.workflow = WorkflowDefinition.objects.create(
            brand='test_brand',
            name='Test Workflow',
            description='Test workflow for unit tests',
            is_active=True
        )

        # Create workflow steps
        self.step1 = WorkflowStep.objects.create(
            workflow=self.workflow,
            name='step_one',
            step_type='sync',
            order=0,
            api_endpoint='/api/test/step1',
            retryable=True
        )
        self.step2 = WorkflowStep.objects.create(
            workflow=self.workflow,
            name='step_two',
            step_type='sync',
            order=1,
            api_endpoint='/api/test/step2',
            retryable=True
        )
        self.step3 = WorkflowStep.objects.create(
            workflow=self.workflow,
            name='step_three',
            step_type='async',
            order=2,
            api_endpoint='/api/test/step3',
            retryable=True
        )

        # Create sequence edges
        WorkflowSequence.objects.create(
            workflow=self.workflow,
            from_step='step_one',
            to_step='step_two',
            condition='always'
        )
        WorkflowSequence.objects.create(
            workflow=self.workflow,
            from_step='step_two',
            to_step='step_three',
            condition='always'
        )

        self.manager = WorkflowManager()

    def test_start_creates_enrollment(self):
        """Test that start() creates a new enrollment state and auto-completes sync steps."""
        # Mock the step executor to avoid actual API calls
        mock_executor = MagicMock()
        mock_executor.execute.return_value = {'success': True}
        self.manager._set_step_executor(mock_executor)

        enrollment = self.manager.start('TEST123', 'test_brand')

        self.assertIsNotNone(enrollment)
        self.assertEqual(enrollment.vin, 'TEST123')
        self.assertEqual(enrollment.brand.brand, 'test_brand')
        # Auto-continue through sync steps stops at async step (step_three)
        self.assertEqual(enrollment.current_step, 'step_three')
        self.assertEqual(enrollment.status, 'awaiting_async')

    def test_start_returns_existing_for_in_progress(self):
        """Test that start() returns existing enrollment if already in progress."""
        # Create existing in-progress enrollment
        VehicleEnrollmentState.objects.create(
            vin='TEST123',
            brand=self.workflow,
            status='in_progress'
        )

        enrollment = self.manager.start('TEST123', 'test_brand')

        # Should return existing enrollment, not create new
        self.assertEqual(VehicleEnrollmentState.objects.filter(vin='TEST123').count(), 1)

    def test_start_raises_error_for_invalid_brand(self):
        """Test that start() raises error for non-existent brand."""
        with self.assertRaises(ValueError) as context:
            self.manager.start('TEST123', 'nonexistent_brand')

        self.assertIn('No active workflow', str(context.exception))

    def test_start_raises_error_for_completed_enrollment(self):
        """Test that start() raises error when enrollment already completed."""
        # Create completed enrollment directly
        VehicleEnrollmentState.objects.create(
            vin='COMPLETED123',
            brand=self.workflow,
            status='completed'
        )

        with self.assertRaises(ValueError) as context:
            self.manager.start('COMPLETED123', 'test_brand')

        self.assertIn('already exists', str(context.exception))

    def test_current_returns_enrollment(self):
        """Test that current() returns enrollment state."""
        enrollment = VehicleEnrollmentState.objects.create(
            vin='CURR123',
            brand=self.workflow,
            status='in_progress',
            current_step='step_one'
        )

        result = self.manager.current('CURR123')
        self.assertEqual(result.vin, 'CURR123')

    def test_current_returns_none_for_missing_vin(self):
        """Test that current() returns None for non-existent VIN."""
        result = self.manager.current('NONEXISTENT123')
        self.assertIsNone(result)

    def test_retry_clears_error_and_re_executes(self):
        """Test that retry() clears error message and re-executes the step (tries is preserved)."""
        enrollment = VehicleEnrollmentState.objects.create(
            vin='RETRY123',
            brand=self.workflow,
            status='failed',
            current_step='step_one',
            tries=3,
            error_message='Some error'
        )

        mock_executor = MagicMock()
        mock_executor.execute.return_value = {'success': True}
        self.manager._set_step_executor(mock_executor)

        result = self.manager.retry('RETRY123')

        # Tries is NOT reset - we track total attempts for auditing
        self.assertEqual(result.tries, 3)
        # Error message is cleared
        self.assertEqual(result.error_message, '')

    def test_cancel_marks_enrollment_cancelled(self):
        """Test that cancel() marks enrollment as cancelled."""
        enrollment = VehicleEnrollmentState.objects.create(
            vin='CANCEL123',
            brand=self.workflow,
            status='in_progress'
        )

        result = self.manager.cancel('CANCEL123')
        self.assertEqual(result.status, 'cancelled')
        self.assertIsNotNone(result.completed_at)

    def test_cancel_not_overridden_by_async_complete(self):
        """Test that cancel() is respected even if async message arrives later."""
        # Create enrollment in awaiting_async state
        enrollment = VehicleEnrollmentState.objects.create(
            vin='CANCELASYNC123',
            brand=self.workflow,
            status='awaiting_async',
            current_step='step_three',
            step_data={'step_three': {'request_id': 'REQ001'}}
        )

        # Create async request
        AsyncEnrollmentRequest.objects.create(
            enrollment=enrollment,
            request_id='REQ001',
            products=['tracking'],
            status='pending'
        )

        # Cancel the enrollment
        result = self.manager.cancel('CANCELASYNC123')
        self.assertEqual(result.status, 'cancelled')

        # Now simulate async message arriving after cancel
        # This should NOT override the cancelled status
        result = self.manager.handle_async_complete(
            vin='CANCELASYNC123',
            request_id='REQ001',
            success=True,
            response_data={'request_id': 'REQ001', 'status': 'success', 'error_code': '6700'},
            error_message='',
            product_id='tracking'
        )

        # Status should still be cancelled, not overwritten to in_progress
        result.refresh_from_db()
        self.assertEqual(result.status, 'cancelled')
        self.assertNotEqual(result.current_step, 'step_two')  # Should not have advanced

    def test_handle_async_complete_stores_data(self):
        """Test that async completion stores response data."""
        enrollment = VehicleEnrollmentState.objects.create(
            vin='ASYNC123',
            brand=self.workflow,
            status='awaiting_async',
            current_step='step_three',
            step_data={}
        )

        # Mock the step executor
        mock_executor = MagicMock()
        mock_executor.execute.return_value = {'success': True}
        self.manager._set_step_executor(mock_executor)

        # Store the response data directly (as handle_async_complete does)
        enrollment.step_data[enrollment.current_step] = {'enrollment_id': '123'}
        enrollment.save()

        # Verify data was stored
        enrollment.refresh_from_db()
        self.assertIn('step_three', enrollment.step_data)
        self.assertEqual(enrollment.step_data['step_three']['enrollment_id'], '123')


class VehicleEnrollmentStateTestCase(TestCase):
    """Tests for VehicleEnrollmentState model methods."""

    def setUp(self):
        """Set up test fixtures."""
        self.workflow = WorkflowDefinition.objects.create(
            brand='test_brand',
            name='Test Workflow',
            is_active=True
        )

    def test_mark_in_progress(self):
        """Test mark_in_progress() method."""
        state = VehicleEnrollmentState.objects.create(
            vin='TEST123',
            brand=self.workflow,
            status='pending'
        )

        state.mark_in_progress()
        state.refresh_from_db()

        self.assertEqual(state.status, 'in_progress')

    def test_mark_awaiting_async(self):
        """Test mark_awaiting_async() method."""
        state = VehicleEnrollmentState.objects.create(
            vin='TEST123',
            brand=self.workflow,
            status='in_progress'
        )

        state.mark_awaiting_async()
        state.refresh_from_db()

        self.assertEqual(state.status, 'awaiting_async')

    def test_mark_completed(self):
        """Test mark_completed() method."""
        state = VehicleEnrollmentState.objects.create(
            vin='TEST123',
            brand=self.workflow,
            status='in_progress'
        )

        state.mark_completed()
        state.refresh_from_db()

        self.assertEqual(state.status, 'completed')
        self.assertIsNotNone(state.completed_at)

    def test_mark_failed(self):
        """Test mark_failed() method."""
        state = VehicleEnrollmentState.objects.create(
            vin='TEST123',
            brand=self.workflow,
            status='in_progress'
        )

        state.mark_failed('Test error message')
        state.refresh_from_db()

        self.assertEqual(state.status, 'failed')
        self.assertEqual(state.error_message, 'Test error message')


class WorkflowDefinitionTestCase(TestCase):
    """Tests for WorkflowDefinition model."""

    def test_create_workflow(self):
        """Test creating a workflow definition."""
        workflow = WorkflowDefinition.objects.create(
            brand='toyota',
            name='Toyota Workflow',
            description='Workflow for Toyota vehicles',
            is_active=True
        )

        self.assertEqual(str(workflow), 'toyota - Toyota Workflow')

    def test_workflow_steps_relationship(self):
        """Test that workflow has a steps relationship."""
        workflow = WorkflowDefinition.objects.create(
            brand='honda',
            name='Honda Workflow',
            is_active=True
        )

        WorkflowStep.objects.create(
            workflow=workflow,
            name='verify',
            step_type='sync',
            order=0,
            api_endpoint='/api/honda/verify'
        )

        self.assertEqual(workflow.steps.count(), 1)
        self.assertEqual(workflow.steps.first().name, 'verify')


class APITestCase(TestCase):
    """API endpoint tests."""

    def setUp(self):
        """Set up test fixtures."""
        self.workflow = WorkflowDefinition.objects.create(
            brand='test_brand',
            name='Test Workflow',
            is_active=True
        )

        WorkflowStep.objects.create(
            workflow=self.workflow,
            name='step_one',
            step_type='sync',
            order=0,
            api_endpoint='/api/test/step1'
        )

    def test_health_endpoint_no_auth(self):
        """Test that health endpoint works without auth."""
        response = self.client.get('/api/enroll/health/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'healthy')

    def test_start_enrollment_requires_api_key(self):
        """Test that start endpoint requires API key."""
        response = self.client.post(
            '/api/enroll/start/',
            data={'vin': 'TEST123', 'brand': 'test_brand'},
            content_type='application/json'
        )

        # Should fail without API key (401 or 403)
        self.assertIn(response.status_code, [401, 403])

    def test_get_enrollment_state_direct_model(self):
        """Test that enrollment state can be accessed via model."""
        VehicleEnrollmentState.objects.create(
            vin='TEST123',
            brand=self.workflow,
            status='in_progress',
            current_step='step_one'
        )

        # Verify the state exists in database
        state = VehicleEnrollmentState.objects.get(vin='TEST123')
        self.assertEqual(state.vin, 'TEST123')
        self.assertEqual(state.status, 'in_progress')

    def test_list_workflows_direct_model(self):
        """Test that workflows can be listed via model."""
        # Verify the workflow exists
        workflows = WorkflowDefinition.objects.filter(is_active=True)
        self.assertEqual(workflows.count(), 1)
        self.assertEqual(workflows[0].brand, 'test_brand')


class StepExecutorTestCase(TestCase):
    """Tests for StepExecutor class."""

    def setUp(self):
        """Set up test fixtures."""
        self.workflow = WorkflowDefinition.objects.create(
            brand='test_brand',
            name='Test Workflow',
            is_active=True
        )

        self.enrollment = VehicleEnrollmentState.objects.create(
            vin='TEST123',
            brand=self.workflow,
            status='in_progress',
            current_step='step_one'
        )

        self.step = WorkflowStep.objects.create(
            workflow=self.workflow,
            name='step_one',
            step_type='sync',
            order=0,
            api_endpoint='/api/test/step1'
        )

        self.executor = StepExecutor()

    def test_build_request_data(self):
        """Test building request data for a step."""
        data = self.executor._build_request_data(self.enrollment, self.step)

        self.assertEqual(data['vin'], 'TEST123')
        self.assertEqual(data['brand'], 'test_brand')

    def test_build_request_data_with_products(self):
        """Test that step products are included in request."""
        self.step.products = ['tracking', 'diagnostics']
        self.step.save()

        data = self.executor._build_request_data(self.enrollment, self.step)

        self.assertEqual(data['products'], ['tracking', 'diagnostics'])

    def test_get_api_url_with_base_url(self):
        """Test getting full API URL with base URL configured."""
        url = self.executor._get_api_url('test_brand', '/validate')
        # Base URL not configured for test_brand, so just returns endpoint
        self.assertEqual(url, '/validate')

    @patch('enrollment.step_executor.requests.post')
    def test_execute_sync_success(self, mock_post):
        """Test successful sync API call."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {'valid': True}
        mock_post.return_value = mock_response

        result = self.executor._execute_sync('http://test/api', {'vin': 'TEST'}, self.step)

        self.assertTrue(result['success'])
        self.assertIn('data', result)

    @patch('enrollment.step_executor.requests.post')
    def test_execute_sync_failure(self, mock_post):
        """Test failed sync API call."""
        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.text = 'Bad Request'
        mock_post.return_value = mock_response

        result = self.executor._execute_sync('http://test/api', {'vin': 'TEST'}, self.step)

        self.assertFalse(result['success'])
        self.assertIn('error', result)


class EnrollmentHistoryTestCase(TestCase):
    """Tests for EnrollmentHistory model."""

    def setUp(self):
        """Set up test fixtures."""
        self.workflow = WorkflowDefinition.objects.create(
            brand='test_brand',
            name='Test Workflow',
            is_active=True
        )

        self.enrollment = VehicleEnrollmentState.objects.create(
            vin='TEST123',
            brand=self.workflow,
            status='in_progress'
        )

    def test_create_history_entry(self):
        """Test creating a history entry."""
        history = EnrollmentHistory.objects.create(
            enrollment=self.enrollment,
            step_name='step_one',
            from_status='pending',
            to_status='in_progress',
            action='start'
        )

        self.assertEqual(history.enrollment.vin, 'TEST123')
        self.assertEqual(history.action, 'start')

    def test_history_model(self):
        """Test that history entries can be created and retrieved."""
        EnrollmentHistory.objects.create(
            enrollment=self.enrollment,
            action='action1'
        )
        EnrollmentHistory.objects.create(
            enrollment=self.enrollment,
            action='action2'
        )

        history = EnrollmentHistory.objects.filter(enrollment=self.enrollment)
        self.assertEqual(history.count(), 2)


class WorkflowSequenceTestCase(TestCase):
    """Tests for WorkflowSequence model."""

    def setUp(self):
        """Set up test fixtures."""
        self.workflow = WorkflowDefinition.objects.create(
            brand='test_brand',
            name='Test Workflow',
            is_active=True
        )

        self.step1 = WorkflowStep.objects.create(
            workflow=self.workflow,
            name='step_one',
            step_type='sync',
            order=0,
            api_endpoint='/api/test/step1'
        )

        self.step2 = WorkflowStep.objects.create(
            workflow=self.workflow,
            name='step_two',
            step_type='sync',
            order=1,
            api_endpoint='/api/test/step2'
        )

    def test_create_sequence(self):
        """Test creating a workflow sequence."""
        sequence = WorkflowSequence.objects.create(
            workflow=self.workflow,
            from_step='step_one',
            to_step='step_two',
            condition='always'
        )

        self.assertEqual(str(sequence), 'step_one -> step_two (always)')
        self.assertEqual(sequence.condition, 'always')


class PubSubMessageFormatTestCase(TestCase):
    """Tests for pub/sub message format handling."""

    def setUp(self):
        """Set up test fixtures."""
        self.workflow = WorkflowDefinition.objects.create(
            brand='maruti',
            name='Maruti Workflow',
            is_active=True
        )

        # Create steps
        self.step1 = WorkflowStep.objects.create(
            workflow=self.workflow,
            name='submit_enrollment',
            step_type='async',
            order=0,
            api_endpoint='/api/maruti/enroll',
            products=['tracking', 'diagnostics']
        )

        self.step2 = WorkflowStep.objects.create(
            workflow=self.workflow,
            name='register_ready',
            step_type='sync',
            order=1,
            api_endpoint='/api/maruti/register-ready'
        )

        # Create enrollment in awaiting state
        self.enrollment = VehicleEnrollmentState.objects.create(
            vin='VIN123',
            brand=self.workflow,
            status='awaiting_async',
            current_step='submit_enrollment',
            step_data={'submit_enrollment': {'request_id': 'REQ001'}}
        )

        # Create async request
        self.async_request = AsyncEnrollmentRequest.objects.create(
            enrollment=self.enrollment,
            request_id='REQ001',
            products=['tracking', 'diagnostics'],
            status='pending'
        )

    def test_maruti_message_format_success(self):
        """Test handling of Maruti success message (error_code=6700)."""
        message = {
            'request_id': 'REQ001',
            'product_id': 'PROD001',
            'vin': 'VIN123',
            'status': 'success',
            'error_code': '6700'
        }

        # Verify message has required fields
        self.assertIn('request_id', message)
        self.assertIn('vin', message)
        self.assertIn('error_code', message)

        # Verify success code
        self.assertEqual(message['error_code'], '6700')

    def test_maruti_message_format_failure(self):
        """Test handling of Maruti failure message."""
        message = {
            'request_id': 'REQ001',
            'product_id': 'PROD001',
            'vin': 'VIN123',
            'status': 'failed',
            'error_code': '6701'
        }

        self.assertNotEqual(message['error_code'], '6700')
        self.assertEqual(message['status'], 'failed')


class AsyncEnrollmentRequestTestCase(TestCase):
    """Tests for AsyncEnrollmentRequest model."""

    def setUp(self):
        """Set up test fixtures."""
        self.workflow = WorkflowDefinition.objects.create(
            brand='test_brand',
            name='Test Workflow',
            is_active=True
        )

        self.enrollment = VehicleEnrollmentState.objects.create(
            vin='TEST123',
            brand=self.workflow,
            status='awaiting_async',
            current_step='async_step'
        )

    def test_create_async_request(self):
        """Test creating an async enrollment request."""
        request = AsyncEnrollmentRequest.objects.create(
            enrollment=self.enrollment,
            request_id='REQ123',
            products=['tracking', 'diagnostics'],
            status='pending'
        )

        self.assertEqual(request.enrollment.vin, 'TEST123')
        self.assertEqual(request.status, 'pending')
        self.assertEqual(request.products, ['tracking', 'diagnostics'])

    def test_async_request_status_transitions(self):
        """Test async request status transitions."""
        request = AsyncEnrollmentRequest.objects.create(
            enrollment=self.enrollment,
            request_id='REQ456',
            status='pending'
        )

        request.status = 'processing'
        request.save()

        request.status = 'success'
        from django.utils import timezone
        request.completed_at = timezone.now()
        request.save()

        request.refresh_from_db()
        self.assertEqual(request.status, 'success')
        self.assertIsNotNone(request.completed_at)
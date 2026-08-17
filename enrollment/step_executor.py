"""
Step Executor - Handles API calls for workflow steps.

Supports:
- Sync requests: Execute and wait for immediate response
- Async requests: Execute, return request ID, wait for callback
"""

import logging
import json
import requests
from typing import Dict, Any, Optional
from django.conf import settings

from .models import VehicleEnrollmentState, WorkflowStep, AsyncEnrollmentRequest

logger = logging.getLogger(__name__)

# Non-retryable error codes (validation/business logic failures)
NON_RETRYABLE_ERROR_CODES = ['5001', '5002', '5003', '5004', '5005']


class StepExecutor:
    """
    Executes workflow steps by calling OEM APIs.
    Handles both sync and async step types.
    """

    def __init__(self):
        self.timeout = 30  # Default timeout for API calls

    def execute(
        self,
        enrollment: VehicleEnrollmentState,
        step: WorkflowStep
    ) -> Dict[str, Any]:
        """
        Execute a workflow step.

        Args:
            enrollment: The vehicle enrollment state
            step: The workflow step to execute

        Returns:
            Dict with 'success' boolean, 'error', and 'retryable' flag
        """
        logger.info(f"Executing step {step.name} for VIN={enrollment.vin}")

        try:
            # Build request data based on step configuration
            request_data = self._build_request_data(enrollment, step)

            # Get API URL
            api_url = self._get_api_url(enrollment.brand.brand, step.api_endpoint)

            if step.step_type == 'sync':
                return self._execute_sync(api_url, request_data, step)
            else:
                return self._execute_async(enrollment, step, request_data)

        except Exception as e:
            logger.exception(f"Error executing step {step.name} for VIN={enrollment.vin}")
            return {'success': False, 'error': str(e), 'retryable': True}

    def _build_request_data(
        self,
        enrollment: VehicleEnrollmentState,
        step: WorkflowStep
    ) -> Dict[str, Any]:
        """Build the request data for a step."""
        data = {
            'vin': enrollment.vin,
            'brand': enrollment.brand.brand,
        }

        # Add capabilities from step config
        if step.capabilities:
            data['capabilities'] = step.capabilities

        # Add products from step config
        if step.products:
            data['products'] = step.products

        # Merge with any existing step data
        if step.name in enrollment.step_data:
            data.update(enrollment.step_data[step.name])

        return data

    def _get_api_url(self, brand: str, endpoint: str) -> str:
        """Get full API URL for an endpoint."""
        base_url = settings.OEM_API_BASE_URL.get(brand, '')
        if base_url:
            return f"{base_url}{endpoint}"
        return endpoint

    def _execute_sync(
        self,
        api_url: str,
        request_data: Dict,
        step: WorkflowStep
    ) -> Dict[str, Any]:
        """
        Execute a synchronous API call.
        Returns immediately with response or error.

        Distinguishes between retryable errors (5xx, timeout) and permanent failures (4xx).
        """
        logger.info(f"Executing sync call to {api_url}")

        try:
            response = requests.post(
                api_url,
                json=request_data,
                timeout=step.timeout_seconds or self.timeout
            )

            if response.status_code >= 200 and response.status_code < 300:
                try:
                    data = response.json()
                    return {'success': True, 'data': data}
                except ValueError:
                    return {'success': True, 'data': response.text}
            else:
                # Parse error response for error_code
                error_code, retryable = self._parse_error_response(response)

                return {
                    'success': False,
                    'error': f"HTTP {response.status_code}: {response.text[:200]}",
                    'error_code': error_code,
                    'retryable': retryable
                }

        except requests.exceptions.Timeout:
            return {'success': False, 'error': f"Timeout after {step.timeout_seconds}s", 'retryable': True}
        except requests.exceptions.ConnectionError:
            return {'success': False, 'error': f"Connection error", 'retryable': True}
        except requests.exceptions.RequestException as e:
            return {'success': False, 'error': str(e), 'retryable': True}

    def _parse_error_response(self, response) -> tuple:
        """
        Parse error response to determine if it's retryable.

        Returns:
            tuple: (error_code: str or None, retryable: bool)
        """
        try:
            data = response.json()
            error_code = data.get('error_code', '')

            # Check if error_code is in non-retryable list
            if error_code and error_code in NON_RETRYABLE_ERROR_CODES:
                return (error_code, False)

            # Check for explicit retryable field
            if data.get('retryable') is False:
                return (error_code, False)

        except (ValueError, AttributeError):
            pass

        # Default: 4xx = non-retryable, 5xx = retryable
        status_code = response.status_code
        if status_code >= 400 and status_code < 500:
            return (None, False)  # Client errors are not retryable
        else:
            return (None, True)   # Server errors are retryable

    def _execute_async(
        self,
        enrollment: VehicleEnrollmentState,
        step: WorkflowStep,
        request_data: Dict
    ) -> Dict[str, Any]:
        """
        Execute an asynchronous API call.
        Creates AsyncEnrollmentRequest record and returns immediately.
        Caller must poll or wait for callback to check completion.
        """
        logger.info(f"Executing async call to {step.api_endpoint} for VIN={enrollment.vin}")

        api_url = self._get_api_url(enrollment.brand.brand, step.api_endpoint)

        try:
            response = requests.post(
                api_url,
                json=request_data,
                timeout=step.timeout_seconds or self.timeout
            )

            if response.status_code >= 200 and response.status_code < 300:
                try:
                    response_data = response.json()
                except ValueError:
                    response_data = {'raw_response': response.text}

                # Create async request record
                request_id = response_data.get('request_id', response_data.get('id', ''))

                if not request_id:
                    # OEM may return success synchronously even for async endpoint
                    return {'success': True, 'data': response_data}

                # Initialize pending_products with all expected products
                expected_products = step.products or []

                AsyncEnrollmentRequest.objects.create(
                    enrollment=enrollment,
                    request_id=str(request_id),
                    products=expected_products,
                    completed_products=[],
                    pending_products=list(expected_products),  # All products start as pending
                    status='pending',
                    response_data=response_data
                )

                return {
                    'success': True,
                    'data': {
                        'request_id': request_id,
                        'message': 'Async request submitted, waiting for completion'
                    }
                }
            else:
                # Parse error response for error_code
                error_code, retryable = self._parse_error_response(response)

                return {
                    'success': False,
                    'error': f"HTTP {response.status_code}: {response.text[:200]}",
                    'error_code': error_code,
                    'retryable': retryable
                }

        except requests.exceptions.Timeout:
            return {'success': False, 'error': f"Timeout after {step.timeout_seconds}s", 'retryable': True}
        except requests.exceptions.ConnectionError:
            return {'success': False, 'error': f"Connection error", 'retryable': True}
        except requests.exceptions.RequestException as e:
            return {'success': False, 'error': str(e), 'retryable': True}

    def check_async_status(self, async_request: AsyncEnrollmentRequest) -> Dict[str, Any]:
        """
        Check the status of an async request.
        Calls OEM's status endpoint to get current state.

        Args:
            async_request: The AsyncEnrollmentRequest to check

        Returns:
            Dict with 'status' ('pending', 'success', 'failed') and optional 'data'
        """
        logger.info(f"Checking async status for request_id={async_request.request_id}")

        enrollment = async_request.enrollment
        api_url = self._get_api_url(
            enrollment.brand.brand,
            f"/status/{async_request.request_id}"
        )

        try:
            response = requests.get(api_url, timeout=30)

            if response.status_code >= 200 and response.status_code < 300:
                try:
                    data = response.json()
                    return {
                        'status': data.get('status', 'pending'),
                        'data': data
                    }
                except ValueError:
                    return {'status': 'pending', 'data': {}}
            else:
                return {
                    'status': 'error',
                    'error': f"HTTP {response.status_code}"
                }

        except requests.exceptions.RequestException as e:
            return {'status': 'error', 'error': str(e)}
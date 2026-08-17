"""
REST API views for vehicle enrollment.
"""

import logging
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from .models import (
    VehicleEnrollmentState,
    EnrollmentHistory,
    WorkflowDefinition,
    AsyncEnrollmentRequest,
)
from .workflow_manager import WorkflowManager
from .step_executor import StepExecutor
from .mock_executor import MockStepExecutor
from .permissions import HasAPIKey
from .serializers import (
    StartEnrollmentSerializer,
    VehicleEnrollmentStateSerializer,
    EnrollmentHistorySerializer,
    WorkflowDefinitionSerializer,
)

logger = logging.getLogger(__name__)


class StartEnrollmentView(APIView):
    """
    POST /api/enroll/start/
    Start a new vehicle enrollment.
    Uses real StepExecutor by default.
    """
    permission_classes = [HasAPIKey]

    def post(self, request):
        serializer = StartEnrollmentSerializer(data=request.data)

        if not serializer.is_valid():
            return Response(
                {'error': 'Invalid request', 'details': serializer.errors},
                status=status.HTTP_400_BAD_REQUEST
            )

        vin = serializer.validated_data['vin']
        brand = serializer.validated_data['brand']
        use_mock = request.data.get('use_mock', False)

        try:
            workflow_manager = WorkflowManager()
            if use_mock:
                workflow_manager._set_step_executor(MockStepExecutor())
                logger.info(f"Using mock executor for VIN={vin}")
            else:
                workflow_manager._set_step_executor(StepExecutor())

            enrollment = workflow_manager.start(vin, brand)
            response_serializer = VehicleEnrollmentStateSerializer(enrollment)

            return Response({
                'message': 'Enrollment started',
                'enrollment': response_serializer.data
            }, status=status.HTTP_201_CREATED)

        except ValueError as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_409_CONFLICT
            )
        except Exception as e:
            logger.exception(f"Error starting enrollment for VIN={vin}")
            return Response(
                {'error': 'Internal server error', 'details': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class EnrollmentStateView(APIView):
    """
    GET /api/enroll/state/<vin>/
    Get current enrollment state for a VIN.
    """
    permission_classes = [HasAPIKey]

    def get(self, request, vin):
        workflow_manager = WorkflowManager()
        enrollment = workflow_manager.current(vin.upper())

        if not enrollment:
            return Response(
                {'error': f'No enrollment found for VIN={vin}'},
                status=status.HTTP_404_NOT_FOUND
            )

        serializer = VehicleEnrollmentStateSerializer(enrollment)
        return Response({'enrollment': serializer.data})


class RetryEnrollmentView(APIView):
    """
    POST /api/enroll/retry/<vin>/
    Retry the current step of an enrollment.
    """
    permission_classes = [HasAPIKey]

    def post(self, request, vin):
        force = request.data.get('force', False)

        try:
            workflow_manager = WorkflowManager()
            workflow_manager._set_step_executor(StepExecutor())

            enrollment = workflow_manager.current(vin.upper())

            if not enrollment:
                return Response(
                    {'error': f'No enrollment found for VIN={vin}'},
                    status=status.HTTP_404_NOT_FOUND
                )

            if not force and enrollment.status not in ['in_progress', 'failed']:
                return Response(
                    {'error': f'Cannot retry enrollment in status={enrollment.status}. Use force=true to override.'},
                    status=status.HTTP_409_CONFLICT
                )

            enrollment = workflow_manager.retry(vin.upper())
            serializer = VehicleEnrollmentStateSerializer(enrollment)

            return Response({
                'message': 'Retry initiated',
                'enrollment': serializer.data
            })

        except ValueError as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_409_CONFLICT
            )
        except Exception as e:
            logger.exception(f"Error retrying enrollment for VIN={vin}")
            return Response(
                {'error': 'Internal server error', 'details': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class CancelEnrollmentView(APIView):
    """
    POST /api/enroll/cancel/<vin>/
    Cancel an enrollment.
    """
    permission_classes = [HasAPIKey]

    def post(self, request, vin):
        try:
            workflow_manager = WorkflowManager()
            enrollment = workflow_manager.cancel(vin.upper())
            serializer = VehicleEnrollmentStateSerializer(enrollment)

            return Response({
                'message': 'Enrollment cancelled',
                'enrollment': serializer.data
            })

        except ValueError as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_404_NOT_FOUND
            )
        except Exception as e:
            logger.exception(f"Error cancelling enrollment for VIN={vin}")
            return Response(
                {'error': 'Internal server error', 'details': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class EnrollmentHistoryView(APIView):
    """
    GET /api/enroll/history/<vin>/
    Get enrollment history for a VIN.
    """
    permission_classes = [HasAPIKey]

    def get(self, request, vin):
        enrollment = VehicleEnrollmentState.objects.filter(vin=vin.upper()).first()

        if not enrollment:
            return Response(
                {'error': f'No enrollment found for VIN={vin}'},
                status=status.HTTP_404_NOT_FOUND
            )

        history = EnrollmentHistory.objects.filter(enrollment=enrollment).order_by('-created_at')
        serializer = EnrollmentHistorySerializer(history, many=True)

        return Response({
            'vin': vin.upper(),
            'history': serializer.data
        })


class ListWorkflowsView(APIView):
    """
    GET /api/enroll/workflows/
    List all available workflows.
    """
    permission_classes = [HasAPIKey]

    def get(self, request):
        workflows = WorkflowDefinition.objects.filter(is_active=True)
        serializer = WorkflowDefinitionSerializer(workflows, many=True)
        return Response({'workflows': serializer.data})


class TriggerNextStepView(APIView):
    """
    POST /api/enroll/next/<vin>/
    Manually trigger the next step in the workflow.
    """
    permission_classes = [HasAPIKey]

    def post(self, request, vin):
        try:
            workflow_manager = WorkflowManager()
            workflow_manager._set_step_executor(StepExecutor())

            enrollment = workflow_manager.next(vin.upper())
            serializer = VehicleEnrollmentStateSerializer(enrollment)

            return Response({
                'message': 'Next step triggered',
                'enrollment': serializer.data
            })

        except ValueError as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_404_NOT_FOUND
            )
        except Exception as e:
            logger.exception(f"Error triggering next step for VIN={vin}")
            return Response(
                {'error': 'Internal server error', 'details': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class HealthCheckView(APIView):
    """
    GET /api/enroll/health/
    Health check endpoint (no auth required).
    """
    permission_classes = []

    def get(self, request):
        return Response({
            'status': 'healthy',
            'service': 'vehicle-enrollment-workflow-manager'
        })


class CompleteAsyncStepView(APIView):
    """
    POST /api/enroll/complete-async/
    Manually trigger async step completion (for testing).

    Simulates receiving a pub/sub message from OEM.
    """
    permission_classes = [HasAPIKey]

    def post(self, request):
        vin = request.data.get('vin')
        request_id = request.data.get('request_id')
        success = request.data.get('success', True)
        error_code = request.data.get('error_code', '6700')
        status_val = request.data.get('status', 'success')
        product_id = request.data.get('product_id')  # Optional product identifier

        if not vin:
            return Response(
                {'error': 'vin is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Find the enrollment
        enrollment = VehicleEnrollmentState.objects.filter(vin=vin.upper()).first()
        if not enrollment:
            return Response(
                {'error': f'No enrollment found for VIN={vin}'},
                status=status.HTTP_404_NOT_FOUND
            )

        if enrollment.status != 'awaiting_async':
            return Response(
                {'error': f'Enrollment is not awaiting async (status={enrollment.status})'},
                status=status.HTTP_409_CONFLICT
            )

        # If no request_id provided, find the latest pending async request for this enrollment
        if not request_id:
            latest_async = AsyncEnrollmentRequest.objects.filter(
                enrollment=enrollment,
                status='pending'
            ).order_by('-created_at').first()

            if not latest_async:
                return Response(
                    {'error': 'No pending async request found for this enrollment'},
                    status=status.HTTP_404_NOT_FOUND
                )

            request_id = latest_async.request_id
            logger.info(f"Auto-detected request_id: {request_id}")

        # Build response data
        response_data = {
            'request_id': request_id,
            'vin': vin.upper(),
            'status': status_val,
            'error_code': error_code,
            'product_id': product_id
        }

        # Check if there are pending products
        async_req = AsyncEnrollmentRequest.objects.filter(
            enrollment=enrollment,
            request_id=str(request_id)
        ).first()

        # For multi-product steps, either provide product_id or use complete_all=true
        complete_all = request.data.get('complete_all', False)
        if async_req and async_req.pending_products and not product_id and not complete_all:
            return Response(
                {'error': f'This step has multiple products pending: {async_req.pending_products}. Please specify product_id or use complete_all=true to complete all.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # If complete_all, mark all products complete (skip per-product handling)
        if complete_all and async_req and async_req.pending_products:
            async_req.mark_all_completed()
            response_data['completed_all'] = True

        # Call handle_async_complete
        workflow_manager = WorkflowManager()
        workflow_manager._set_step_executor(MockStepExecutor())

        result = workflow_manager.handle_async_complete(
            vin=vin.upper(),
            request_id=request_id,
            success=success,
            response_data=response_data,
            error_message='' if success else f'Error code: {error_code}',
            product_id=product_id
        )

        serializer = VehicleEnrollmentStateSerializer(result)

        return Response({
            'message': 'Async step completed',
            'enrollment': serializer.data
        })


class PendingAsyncRequestsView(APIView):
    """
    GET /api/enroll/pending/
    List all enrollments waiting for async completion.
    """
    permission_classes = [HasAPIKey]

    def get(self, request):
        pending = VehicleEnrollmentState.objects.filter(
            status='awaiting_async'
        ).select_related('brand')

        data = []
        for enrollment in pending:
            async_req = AsyncEnrollmentRequest.objects.filter(
                enrollment=enrollment
            ).first()

            data.append({
                'vin': enrollment.vin,
                'brand': enrollment.brand.brand,
                'current_step': enrollment.current_step,
                'async_request_id': async_req.request_id if async_req else None,
                'created_at': enrollment.created_at.isoformat()
            })

        return Response({
            'pending_count': len(data),
            'enrollments': data
        })


class EnrollmentStatusAPIView(APIView):
    """
    GET /api/enroll/status/<vin>/ - Combined state + history (for UI, no auth required)
    POST /api/enroll/status/<vin>/complete - Complete async step (for UI, no auth required)
    """
    permission_classes = []

    def get(self, request, vin):
        enrollment = VehicleEnrollmentState.objects.filter(vin=vin.upper()).first()

        if not enrollment:
            return Response(
                {'error': f'No enrollment found for VIN={vin}'},
                status=status.HTTP_404_NOT_FOUND
            )

        state_serializer = VehicleEnrollmentStateSerializer(enrollment)
        history = EnrollmentHistory.objects.filter(enrollment=enrollment).order_by('created_at')
        history_serializer = EnrollmentHistorySerializer(history, many=True)

        async_requests = AsyncEnrollmentRequest.objects.filter(enrollment=enrollment)
        async_data = [{
            'request_id': ar.request_id,
            'products': ar.products,
            'completed_products': ar.completed_products,
            'pending_products': ar.pending_products,
            'status': ar.status,
            'created_at': ar.created_at.isoformat(),
            'completed_at': ar.completed_at.isoformat() if ar.completed_at else None
        } for ar in async_requests]

        return Response({
            'enrollment': state_serializer.data,
            'history': history_serializer.data,
            'async_requests': async_data
        })

    def post(self, request, vin):
        """Complete all pending async products for a VIN (for UI testing)."""
        product_id = request.data.get('product_id')

        enrollment = VehicleEnrollmentState.objects.filter(vin=vin.upper()).first()
        if not enrollment:
            return Response({'error': f'No enrollment found for VIN={vin}'}, status=status.HTTP_404_NOT_FOUND)

        if enrollment.status != 'awaiting_async':
            return Response({'error': f'Not awaiting async (status={enrollment.status})'}, status=status.HTTP_409_CONFLICT)

        async_req = AsyncEnrollmentRequest.objects.filter(enrollment=enrollment).first()
        if not async_req:
            return Response({'error': 'No async request found'}, status=status.HTTP_404_NOT_FOUND)

        # Complete a single product or all products
        if product_id:
            all_done = async_req.mark_product_completed(product_id, {'product_id': product_id, 'status': 'success'})
            step_key = enrollment.current_step
            if step_key not in enrollment.step_data:
                enrollment.step_data[step_key] = {}
            if 'product_responses' not in enrollment.step_data[step_key]:
                enrollment.step_data[step_key]['product_responses'] = {}
            enrollment.step_data[step_key]['product_responses'][product_id] = {'product_id': product_id, 'status': 'success'}

            if all_done:
                enrollment.status = 'in_progress'
                enrollment.save()
                wm = WorkflowManager()
                enrollment = wm.next(vin.upper())

            state_serializer = VehicleEnrollmentStateSerializer(enrollment)
            return Response({'enrollment': state_serializer.data, 'all_done': all_done})
        else:
            # Complete all products at once
            products = async_req.products or []
            for prod in products:
                async_req.mark_product_completed(prod, {'product_id': prod, 'status': 'success'})
            async_req.mark_all_completed()

            step_key = enrollment.current_step
            enrollment.step_data[step_key] = {'product_responses': {prod: {'product_id': prod, 'status': 'success'} for prod in products}}
            enrollment.status = 'in_progress'
            enrollment.save()

            wm = WorkflowManager()
            enrollment = wm.next(vin.upper())

            state_serializer = VehicleEnrollmentStateSerializer(enrollment)
            return Response({'enrollment': state_serializer.data, 'all_done': True})
"""
URL routes for the enrollment API.
"""

from django.urls import path
from django.views.generic import TemplateView
from .views import (
    StartEnrollmentView,
    EnrollmentStateView,
    RetryEnrollmentView,
    CancelEnrollmentView,
    EnrollmentHistoryView,
    ListWorkflowsView,
    TriggerNextStepView,
    HealthCheckView,
    CompleteAsyncStepView,
    PendingAsyncRequestsView,
    EnrollmentStatusAPIView,
)

urlpatterns = [
    # Health check (no auth)
    path('health/', HealthCheckView.as_view(), name='health-check'),

    # UI - Vehicle status page (no auth)
    path('status/', TemplateView.as_view(template_name='enrollment/status.html'), name='enrollment-status'),
    path('status/<str:vin>/', EnrollmentStatusAPIView.as_view(), name='enrollment-status-api'),
    path('status/<str:vin>/complete', EnrollmentStatusAPIView.as_view(), name='enrollment-status-complete'),

    # Start enrollment (add "use_mock": true for testing without OEM APIs)
    path('start/', StartEnrollmentView.as_view(), name='start-enrollment'),

    # Get workflow list
    path('workflows/', ListWorkflowsView.as_view(), name='list-workflows'),

    # Get enrollment state
    path('state/<str:vin>/', EnrollmentStateView.as_view(), name='enrollment-state'),

    # Retry enrollment
    path('retry/<str:vin>/', RetryEnrollmentView.as_view(), name='retry-enrollment'),

    # Cancel enrollment
    path('cancel/<str:vin>/', CancelEnrollmentView.as_view(), name='cancel-enrollment'),

    # Get enrollment history
    path('history/<str:vin>/', EnrollmentHistoryView.as_view(), name='enrollment-history'),

    # Manually trigger next step
    path('next/<str:vin>/', TriggerNextStepView.as_view(), name='trigger-next'),

    # Complete async step (for testing - simulates pub/sub message)
    path('complete-async/', CompleteAsyncStepView.as_view(), name='complete-async'),

    # List pending async enrollments
    path('pending/', PendingAsyncRequestsView.as_view(), name='pending-async'),
]
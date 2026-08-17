"""
Serializers for the enrollment API.
"""

from rest_framework import serializers
from .models import (
    WorkflowDefinition,
    WorkflowStep,
    VehicleEnrollmentState,
    AsyncEnrollmentRequest,
    EnrollmentHistory,
)


class WorkflowStepSerializer(serializers.ModelSerializer):
    """Serializer for workflow steps."""

    class Meta:
        model = WorkflowStep
        fields = ['name', 'step_type', 'order', 'api_endpoint',
                  'capabilities', 'products', 'timeout_seconds', 'retryable']


class WorkflowDefinitionSerializer(serializers.ModelSerializer):
    """Serializer for workflow definitions."""
    steps = WorkflowStepSerializer(many=True, read_only=True)

    class Meta:
        model = WorkflowDefinition
        fields = ['brand', 'name', 'description', 'is_active', 'steps']


class AsyncEnrollmentRequestSerializer(serializers.ModelSerializer):
    """Serializer for async enrollment requests."""

    class Meta:
        model = AsyncEnrollmentRequest
        fields = ['request_id', 'products', 'status', 'response_data',
                  'error_message', 'created_at', 'completed_at']


class EnrollmentHistorySerializer(serializers.ModelSerializer):
    """Serializer for enrollment history."""

    class Meta:
        model = EnrollmentHistory
        fields = ['step_name', 'from_status', 'to_status', 'action', 'details', 'created_at']


class VehicleEnrollmentStateSerializer(serializers.ModelSerializer):
    """Serializer for vehicle enrollment state."""
    brand_name = serializers.CharField(source='brand.brand', read_only=True)
    async_requests = AsyncEnrollmentRequestSerializer(many=True, read_only=True)

    class Meta:
        model = VehicleEnrollmentState
        fields = ['vin', 'brand_name', 'current_step', 'status', 'tries',
                  'max_tries', 'step_data', 'error_message', 'created_at',
                  'updated_at', 'completed_at', 'async_requests']


class StartEnrollmentSerializer(serializers.Serializer):
    """Serializer for starting a new enrollment."""
    vin = serializers.CharField(max_length=50)
    brand = serializers.CharField(max_length=50)

    def validate_vin(self, value):
        """Validate VIN format."""
        if not value or len(value) < 3:
            raise serializers.ValidationError("VIN must be at least 3 characters")
        return value.upper()

    def validate_brand(self, value):
        """Validate brand exists."""
        if not WorkflowDefinition.objects.filter(brand=value, is_active=True).exists():
            raise serializers.ValidationError(f"No active workflow for brand '{value}'")
        return value


class RetryEnrollmentSerializer(serializers.Serializer):
    """Serializer for retry request."""
    force = serializers.BooleanField(default=False, help_text="Force retry even if not in failed state")
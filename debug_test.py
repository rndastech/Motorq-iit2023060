"""
Debug test script with verbose logging
"""

import os
import sys
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
import django
django.setup()

import logging
logging.basicConfig(level=logging.INFO)

from enrollment.models import VehicleEnrollmentState, WorkflowDefinition, WorkflowStep, WorkflowSequence
from enrollment.workflow_manager import WorkflowManager
from enrollment.mock_executor import MockStepExecutor

print("=" * 70)
print("DEBUG TEST")
print("=" * 70)

# Check state
print("\n1. Checking database state...")
VehicleEnrollmentState.objects.filter(vin__in=['TEST123', 'TATA456']).delete()
print("   Cleared any existing test data")

# Check workflows
print("\n2. Checking Maruti workflow...")
workflow = WorkflowDefinition.objects.get(brand='maruti')
print(f"   Workflow: {workflow.brand}")

steps = WorkflowStep.objects.filter(workflow=workflow).order_by('order')
print("   Steps:")
for s in steps:
    print(f"     - {s.order}. {s.name} ({s.step_type})")

sequences = WorkflowSequence.objects.filter(workflow=workflow).order_by('order')
print("   Sequences:")
for s in sequences:
    print(f"     - {s.from_step} -> {s.to_step} ({s.condition})")

# Test start
print("\n3. Starting enrollment...")
wm = WorkflowManager()
wm._set_step_executor(MockStepExecutor())

result = wm.start('TEST123', 'maruti')

print(f"\n4. Result:")
print(f"   VIN: {result.vin}")
print(f"   Status: {result.status}")
print(f"   Current Step: {result.current_step}")
print(f"   Step Data Keys: {list(result.step_data.keys())}")

# Check DB
print("\n5. Checking DB directly...")
state = VehicleEnrollmentState.objects.get(vin='TEST123')
print(f"   DB Status: {state.status}")
print(f"   DB Step: {state.current_step}")
print(f"   DB Step Data: {state.step_data}")
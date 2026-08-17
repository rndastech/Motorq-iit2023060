"""
Integrated test script - runs everything in one process.
No Redis required!

Usage:
    python run_test.py
"""

import os
import sys
import time
import json

# Set fake Redis mode
os.environ['USE_FAKE_REDIS'] = 'true'
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

import django
django.setup()

from enrollment.workflow_manager import WorkflowManager
from enrollment.step_executor import StepExecutor
from enrollment.models import VehicleEnrollmentState, AsyncEnrollmentRequest

# Import fakeredis
import fakeredis

# Shared fake Redis instance
fake_redis = fakeredis.FakeRedis(decode_responses=True)


def simulate_async_completion(vin: str, request_id: str):
    """Simulate receiving a pub/sub message - marks async as complete."""
    print(f"\n[SUB] Simulating async completion for VIN={vin}")
    print(f"[SUB] Message: request_id={request_id}, status=success, error_code=6700")

    enrollment = VehicleEnrollmentState.objects.filter(
        vin=vin,
        status='awaiting_async'
    ).first()

    if not enrollment:
        print(f"[SUB] No awaiting enrollment found for VIN={vin}")
        return

    # Update async request
    async_req = AsyncEnrollmentRequest.objects.filter(
        enrollment=enrollment,
        request_id=request_id
    ).first()

    if async_req:
        async_req.status = 'success'
        async_req.response_data = {'status': 'success', 'error_code': '6700'}
        from django.utils import timezone
        async_req.completed_at = timezone.now()
        async_req.save()

    # Continue workflow (use MockStepExecutor for any remaining sync steps)
    from enrollment.mock_executor import MockStepExecutor
    wm = WorkflowManager()
    wm._set_step_executor(MockStepExecutor())

    result = wm.handle_async_complete(
        vin=vin,
        request_id=request_id,
        success=True,
        response_data={'status': 'success', 'error_code': '6700'}
    )

    print(f"[SUB] Workflow continued. Status: {result.status}, Step: {result.current_step}")


def run_test():
    print("=" * 70)
    print("  INTEGRATED TEST - No Redis Required")
    print("=" * 70)

    from enrollment.mock_executor import MockStepExecutor
    from enrollment.models import VehicleEnrollmentState, AsyncEnrollmentRequest

    # Clear test data
    VehicleEnrollmentState.objects.filter(vin__in=['TEST123', 'TATA456']).delete()
    AsyncEnrollmentRequest.objects.filter(enrollment__vin__in=['TEST123', 'TATA456']).delete()
    print("[CLEARED] Previous test data removed\n")

    # Step 1: Start enrollment
    print("\n[1] Starting enrollment for VIN=TEST123, brand=maruti")
    print("-" * 70)

    wm = WorkflowManager()
    wm._set_step_executor(MockStepExecutor())  # Use mock for testing

    result = wm.start('TEST123', 'maruti')

    print(f"\n[RESULT] After start():")
    print(f"  VIN: {result.vin}")
    print(f"  Status: {result.status}")
    print(f"  Current Step: {result.current_step}")
    print(f"  Step Data Keys: {list(result.step_data.keys())}")

    if result.status == 'awaiting_async':
        # Get the request_id for next step
        async_req = AsyncEnrollmentRequest.objects.filter(
            enrollment=result
        ).first()

        if async_req:
            print(f"\n[2] Async request submitted: {async_req.request_id}")
            print(f"    Products: {async_req.products}")

            # Step 3: Simulate async completion (like pub/sub message)
            print("\n[3] Simulating async completion (like receiving pub/sub message)")
            print("-" * 70)

            simulate_async_completion('TEST123', async_req.request_id)

            # Check final state
            final_state = VehicleEnrollmentState.objects.get(vin='TEST123')
            print(f"\n[FINAL] Enrollment state:")
            print(f"  Status: {final_state.status}")
            print(f"  Current Step: {final_state.current_step}")
            print(f"  Step Data:")
            for step, data in final_state.step_data.items():
                if isinstance(data, list):
                    print(f"    {step}: [{len(data)} completions]")
                else:
                    print(f"    {step}: {data}")

            if final_state.status == 'completed':
                print("\n" + "=" * 70)
                print("  ✓ ENROLLMENT COMPLETED SUCCESSFULLY!")
                print("=" * 70)
            else:
                print(f"\n[WARNING] Enrollment not completed. Status: {final_state.status}")
    else:
        print(f"\n[INFO] Enrollment already completed (no async step): {result.status}")

    # Test Tata workflow too
    print("\n\n" + "=" * 70)
    print("  TESTING TATA WORKFLOW (with two async steps)")
    print("=" * 70)

    print("\n[1] Starting enrollment for VIN=TATA456, brand=tata")
    print("-" * 70)

    wm2 = WorkflowManager()
    wm2._set_step_executor(MockStepExecutor())
    result2 = wm2.start('TATA456', 'tata')
    print(f"\n[RESULT] After start():")
    print(f"  Status: {result2.status}")
    print(f"  Current Step: {result2.current_step}")

    if result2.status == 'awaiting_async':
        # Continue until all async steps are completed
        step_count = 0
        processed_request_ids = set()

        while True:
            current = VehicleEnrollmentState.objects.get(vin='TATA456')
            if current.status != 'awaiting_async':
                break

            # Get the current step's async request
            current_step = current.current_step
            async_req = AsyncEnrollmentRequest.objects.filter(
                enrollment=current,
                request_id__contains=current_step
            ).first()

            if not async_req:
                print(f"[ERROR] No async request found for step: {current_step}")
                break

            if async_req.request_id in processed_request_ids:
                print(f"[INFO] Already processed request: {async_req.request_id}")
                break

            processed_request_ids.add(async_req.request_id)
            step_count += 1
            print(f"\n[{2 + step_count}] Simulating completion for: {async_req.request_id}")
            simulate_async_completion('TATA456', async_req.request_id)

            # Safety limit
            if step_count > 10:
                print("[ERROR] Too many iterations, breaking")
                break

        # Final state
        final = VehicleEnrollmentState.objects.get(vin='TATA456')
        print(f"\n[FINAL TATA] Status: {final.status}, Step: {final.current_step}")

        if final.status == 'completed':
            print("\n" + "=" * 70)
            print("  ✓ TATA ENROLLMENT COMPLETED SUCCESSFULLY!")
            print("=" * 70)


if __name__ == '__main__':
    run_test()
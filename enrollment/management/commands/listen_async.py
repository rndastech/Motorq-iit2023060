"""
Management command to run the pub/sub listener for async enrollment completion.
"""

from enrollment.pubsub import Command as PubSubCommand


# Re-export the command from pubsub module
class Command(PubSubCommand):
    """Django management command to run the pub/sub listener."""
    pass
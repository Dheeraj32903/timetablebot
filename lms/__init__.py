"""
KL University LMS (Moodle) integration package.
Provides asynchronous client, task parsing, deadline detection, and Telegram formatting.
"""

from lms.client import LMSClient
from lms.service import LMSService, LMSTask

__all__ = ["LMSClient", "LMSService", "LMSTask"]

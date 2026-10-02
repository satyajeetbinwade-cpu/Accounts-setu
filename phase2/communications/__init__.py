"""Communications module — WhatsApp and email follow-ups.

Sends automated follow-up messages to clients for:
- Missing invoices
- Missing entries / context
- Custom messages from the platform

Usage:
    from src.communications import service as comms
    comms.init_communications()
    comms.send_followup(...)
"""

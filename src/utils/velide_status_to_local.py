from api.sqlite_manager import DeliveryStatus as LocalStatus

def map_velide_status_to_local(velide_status: str) -> LocalStatus:
    """
    Translates Velide GraphQL Status strings to Local SQLite Enums.
    """
    # Normalize input (uppercase, stripped)
    v_status = velide_status.strip().upper()

    mapping = {
        "PENDING": LocalStatus.ADDED,         # Waiting for assignment
        "ROUTED": LocalStatus.IN_PROGRESS,    # Left the warehouse
        "COMPLETED": LocalStatus.DELIVERED,   # Success
        "CANCELLED": LocalStatus.CANCELLED,   # Cancelled
        "FAILED": LocalStatus.FAILED,         # Delivery exception
    }
    
    # Default to PENDING if unknown, or raise error depending on strictness
    return mapping.get(v_status, LocalStatus.PENDING)
from typing import Dict, List, Tuple
from models.base_models import BaseLocalDeliveryman
from models.velide_delivery_models import DeliverymanResponse
# from rapidfuzz import process, fuzz

def generate_levenshtein_mappings(
    source_items: List[DeliverymanResponse],
    destination_options: List[BaseLocalDeliveryman]
) -> Dict[str, str]:
    """
    Generates a default mapping by finding the closest Levenshtein match.

    It maps source.id -> destination.id.

    Args:
        source_items: The list of source objects (DeliverymanResponse).
        destination_options: The list of destination objects (float, str).
    Returns:
        A dictionary mapping source IDs to the best-matching destination name.
    """
    default_mappings: Dict[str, str] = {}
    
    # Extract just the names from the destination options.
    # This is the list of "choices" we will match against.
    destination_names = [opt.name for opt in destination_options]

    if not destination_names:
        # Cannot map if there are no options
        return {}

    # for source in source_items:
    #     # Use rapidfuzz to find the single best match for the source name
    #     # from the list of destination names.
    #     # It returns a tuple: (match_string, score, index)
    #     best_match = process.extractOne(
    #         source.name,
    #         destination_names,
    #         scorer=fuzz.ratio # Use standard Levenshtein ratio
    #     )

    #     if best_match:
    #         match_name, score, index = best_match
            
    #         # Only create the mapping if the match is good enough
    #         default_mappings[source.id] = match_name
                
    return default_mappings
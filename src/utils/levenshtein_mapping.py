import difflib
from typing import Dict, List, Optional
from models.base_models import BaseLocalDeliveryman
from models.velide_delivery_models import DeliverymanResponse

# A threshold for how "good" a match needs to be (0.0 to 1.0)
# 0.7 means 70% similar. Adjust as needed.
MIN_MATCH_SCORE = 0


def get_best_match(query: str, choices: List[str]) -> Optional[str]:
    """
    Finds the single best match for a query string from a list of choices.

    Uses difflib.SequenceMatcher to find the highest similarity ratio.
    """
    best_score = -1.0
    best_match = None

    for choice in choices:
        # Create a matcher and get the similarity ratio
        s = difflib.SequenceMatcher(None, query, choice)
        score = s.ratio()

        if score > best_score:
            best_score = score
            best_match = choice

    # Only return the match if it's above our minimum threshold
    if best_score >= MIN_MATCH_SCORE:
        return best_match
    return None


def generate_levenshtein_mappings(
    source_items: List[DeliverymanResponse],
    destination_options: List[BaseLocalDeliveryman],
) -> Dict[str, str]:
    """
    Generates a default mapping by finding the closest string match using difflib.

    It maps source.id -> destination.name.

    Args:
        source_items: The list of source objects (DeliverymanResponse).
        destination_options: The list of destination objects (BaseLocalDeliveryman).
    Returns:
        A dictionary mapping source IDs to the best-matching destination name.
    """
    default_mappings: Dict[str, str] = {}

    destination_names = [opt.name for opt in destination_options]

    if not destination_names:
        return {}

    # Iterate over each source item
    for source in source_items:
        # Find the single best match from the destination names
        best_match_name = get_best_match(source.name, destination_names)

        if best_match_name:
            # If a sufficiently good match was found, create the mapping
            default_mappings[source.id] = best_match_name

    return default_mappings

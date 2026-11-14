class FarmaxEvidenceError(ValueError):
    kind = "SOURCE_CONFLICT"


class FarmaxWaitingError(FarmaxEvidenceError):
    kind = "WAITING_FOR_SOURCE"


class FarmaxMappingMissing(FarmaxEvidenceError):
    kind = "MAPPING_MISSING"

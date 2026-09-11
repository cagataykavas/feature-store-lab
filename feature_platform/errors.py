"""Typed errors exposed by the feature platform service boundary."""


class FeaturePlatformError(Exception):
    """Base class for expected domain failures."""


class FeatureViewNotFound(FeaturePlatformError):
    pass


class FeatureValidationError(FeaturePlatformError):
    pass


class IncompatibleSchemaError(FeaturePlatformError):
    pass


class FeatureMutationError(FeaturePlatformError):
    """A logical feature row was rewritten with different content."""


class MaterializationLeaseError(FeaturePlatformError):
    pass

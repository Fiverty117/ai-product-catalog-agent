# Services layer

Application services coordinate deterministic use cases such as intake, normalization, review, pricing, approval, snapshot creation and catalog rendering.

External AI calls should be reached through dedicated adapters rather than imported throughout the codebase.

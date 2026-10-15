"""Independent native API preflight for the real-window test harness."""


LIQUID_SELECTORS = (
    "setStyle:", "setCornerRadius:", "setEffectIsInteractive:",
    "setAppearance:", "setAlphaValue:",
)


def liquid_api_gaps(appkit):
    cls = getattr(appkit, "NSGlassEffectView", None)
    if cls is None:
        return ["NSGlassEffectView"]
    gaps = [name for name in ("NSGlassEffectViewStyleRegular", "NSAppearanceNameDarkAqua")
            if not hasattr(appkit, name)]
    gaps.extend(selector for selector in LIQUID_SELECTORS
                if not cls.instancesRespondToSelector_(selector))
    return gaps

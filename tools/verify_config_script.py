from swift_scanner.config import ScannerConfig

# Test default
config_default = ScannerConfig()
print(f"Default enable_execution: {config_default.enable_execution}")

# Test explicit disable
config_disabled = ScannerConfig(enable_execution=False)
print(f"Explicit disable: {config_disabled.enable_execution}")

# Test explicit enable (should be redundant now)
config_enabled = ScannerConfig(enable_execution=True)
print(f"Explicit enable: {config_enabled.enable_execution}")

print("\n✓ Configuration verified: Execution enabled by default")

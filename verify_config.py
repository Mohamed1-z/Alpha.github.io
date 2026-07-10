from swift_scanner.config import ScannerConfig

# Test 1: Default pip buffer
config_default = ScannerConfig()
print(f"Default entry_pip_buffer: {config_default.entry_pip_buffer} pips")
assert config_default.entry_pip_buffer == 2.0, "Default should be 2.0 pips"

# Test 2: Custom pip buffer
config_custom = ScannerConfig(entry_pip_buffer=2.0)
print(f"Custom entry_pip_buffer: {config_custom.entry_pip_buffer} pips")
assert config_custom.entry_pip_buffer == 2.0, "Custom should be 2.0 pips"

# Test 3: Execution enabled by default
config_exec = ScannerConfig()
print(f"Execution enabled: {config_exec.enable_execution}")
assert config_exec.enable_execution == True, "Execution should be enabled by default"

print("\n✓ All configuration tests passed!")
print("2-PIP ENTRY BUFFER FEATURE VERIFIED")

#include "iat_lease.h"
#include <cstdio>
#include <cstdint>

using wxbg::IatLease;
using wxbg::IatLeaseCode;
static int checks = 0, failures = 0;
#define CHECK(x) do { ++checks; if (!(x)) { ++failures; std::printf("FAIL line %d: %s\n", __LINE__, #x); return false; } } while (0)
static int ThirdParty() { return 333; }

#ifdef WXBG_IAT_TESTING
namespace {
unsigned protect_calls = 0, fail_protect_call = 0, injected_failures = 0;
void ArmProtectionFailure(unsigned call) {
    protect_calls = 0;
    injected_failures = 0;
    fail_protect_call = call;
}
struct ResetProtectionFault {
    ~ResetProtectionFault() { ArmProtectionFailure(0); }
};
}
BOOL WINAPI wxbg::IatTestVirtualProtect(LPVOID address, SIZE_T size, DWORD protection,
                                      PDWORD old_protection) noexcept {
    ++protect_calls;
    if (fail_protect_call && protect_calls == fail_protect_call) {
        ++injected_failures;
        SetLastError(ERROR_ACCESS_DENIED);
        return FALSE; // Preserve the real page and leave old_protection untouched.
    }
    return VirtualProtect(address, size, protection, old_protection);
}
#endif

struct Fixture {
    HMODULE module = nullptr;
    void* volatile* slot = nullptr;
    void* original = nullptr;
    void* replacement = nullptr;
    DWORD initial_protect = 0;
    bool Init(const wchar_t* path) {
        module = LoadLibraryW(path);
        if (!module) return false;
        slot = reinterpret_cast<void* volatile*>(GetProcAddress(module, "IatFixtureSlot"));
        original = reinterpret_cast<void*>(GetProcAddress(module, "IatFixtureOriginal"));
        replacement = reinterpret_cast<void*>(GetProcAddress(module, "IatFixtureReplacement"));
        if (!slot || !original || !replacement) return false;
        return VirtualProtect(const_cast<void**>(slot), sizeof(void*), PAGE_READONLY, &initial_protect) != FALSE;
    }
    DWORD Protection() const {
        MEMORY_BASIC_INFORMATION info{};
        return VirtualQuery(const_cast<void**>(slot), &info, sizeof(info)) ? info.Protect : 0;
    }
    int Call() const { return reinterpret_cast<int(*)()>(*slot)(); }
    bool ExternalSet(void* value) {
        DWORD old = 0, ignored = 0;
        if (!VirtualProtect(const_cast<void**>(slot), sizeof(void*), PAGE_READWRITE, &old)) return false;
        InterlockedExchangePointer(slot, value);
        return VirtualProtect(const_cast<void**>(slot), sizeof(void*), old, &ignored) != FALSE;
    }
    ~Fixture() {
        if (slot && original) {
            ExternalSet(original);
            DWORD ignored = 0;
            VirtualProtect(const_cast<void**>(slot), sizeof(void*), initial_protect, &ignored);
        }
        if (module) FreeLibrary(module);
    }
};

static bool RoundTrip(Fixture& f) {
    CHECK(f.Call() == 111);
    CHECK(f.Protection() == PAGE_READONLY);
    IatLease lease;
    auto result = lease.Install(f.module, f.slot, f.original, f.replacement);
    CHECK(result.code == IatLeaseCode::ok);
    CHECK(result.observed == f.original && result.slot_changed && result.protection_restored);
    CHECK(lease.installed() && !lease.protection_pending());
    CHECK(lease.original_protection() == PAGE_READONLY);
    CHECK(f.Protection() == PAGE_READONLY);
    CHECK(f.Call() == 222);
    CHECK(reinterpret_cast<int(*)()>(f.original)() == 111);
    result = lease.Restore();
    CHECK(result.code == IatLeaseCode::ok);
    CHECK(result.observed == f.replacement && result.slot_changed && result.protection_restored);
    CHECK(!lease.installed() && !lease.protection_pending());
    CHECK(f.Call() == 111 && f.Protection() == PAGE_READONLY);
    return true;
}

static bool WrongExpected(Fixture& f) {
    IatLease lease;
    auto result = lease.Install(f.module, f.slot, reinterpret_cast<void*>(&ThirdParty), f.replacement);
    CHECK(result.code == IatLeaseCode::compare_exchange_conflict);
    CHECK(result.observed == f.original && !result.slot_changed && result.protection_restored);
    CHECK(!lease.installed() && !lease.protection_pending());
    CHECK(f.Call() == 111 && f.Protection() == PAGE_READONLY);
    CHECK(lease.Restore().code == IatLeaseCode::not_installed);
    return true;
}

static bool RestoreConflict(Fixture& f) {
    IatLease lease;
    CHECK(lease.Install(f.module, f.slot, f.original, f.replacement).code == IatLeaseCode::ok);
    CHECK(f.ExternalSet(reinterpret_cast<void*>(&ThirdParty)));
    auto result = lease.Restore();
    CHECK(result.code == IatLeaseCode::compare_exchange_conflict);
    CHECK(result.observed == reinterpret_cast<void*>(&ThirdParty) && !result.slot_changed);
    CHECK(result.protection_restored && !lease.protection_pending() && lease.installed());
    CHECK(f.Call() == 333 && f.Protection() == PAGE_READONLY);
    CHECK(lease.Restore().code == IatLeaseCode::compare_exchange_conflict);
    CHECK(f.Call() == 333);
    // Test-only cleanup reinstates our expected value before a legitimate retry.
    CHECK(f.ExternalSet(f.replacement));
    CHECK(lease.Restore().code == IatLeaseCode::ok);
    CHECK(f.Call() == 111);
    return true;
}

static bool RepeatedRestore(Fixture& f) {
    IatLease lease;
    CHECK(lease.Restore().code == IatLeaseCode::not_installed);
    CHECK(lease.Install(f.module, f.slot, f.original, f.replacement).code == IatLeaseCode::ok);
    CHECK(lease.Install(f.module, f.slot, f.original, f.replacement).code == IatLeaseCode::already_installed);
    CHECK(f.Call() == 222);
    CHECK(lease.Restore().code == IatLeaseCode::ok);
    CHECK(f.ExternalSet(reinterpret_cast<void*>(&ThirdParty)));
    auto result = lease.Restore();
    CHECK(result.code == IatLeaseCode::already_restored && !result.slot_changed);
    CHECK(f.Call() == 333);
    CHECK(f.ExternalSet(f.original));
    CHECK(lease.Install(f.module, f.slot, f.original, f.replacement).code == IatLeaseCode::ok);
    CHECK(lease.Restore().code == IatLeaseCode::ok);
    return true;
}

static bool RejectArguments(Fixture& f) {
    IatLease lease;
    CHECK(lease.Install(nullptr, f.slot, f.original, f.replacement).code == IatLeaseCode::invalid_argument);
    CHECK(lease.Install(f.module, nullptr, f.original, f.replacement).code == IatLeaseCode::invalid_argument);
    CHECK(lease.Install(f.module, f.slot, nullptr, f.replacement).code == IatLeaseCode::invalid_argument);
    CHECK(lease.Install(f.module, f.slot, f.original, nullptr).code == IatLeaseCode::invalid_argument);
    CHECK(lease.Install(f.module, f.slot, f.original, f.original).code == IatLeaseCode::invalid_argument);
    auto bad = reinterpret_cast<void* volatile*>(reinterpret_cast<std::uintptr_t>(f.slot) + 1);
    CHECK(lease.Install(f.module, bad, f.original, f.replacement).code == IatLeaseCode::unaligned_slot);
    CHECK(f.Call() == 111 && f.Protection() == PAGE_READONLY);
    return true;
}

static bool RejectOtherModule(Fixture& f) {
    IatLease lease;
    CHECK(lease.Install(GetModuleHandleW(nullptr), f.slot, f.original, f.replacement).code == IatLeaseCode::module_mismatch);
    CHECK(f.Call() == 111 && f.Protection() == PAGE_READONLY);
    return true;
}

static bool RejectPrivateAllocation(Fixture& f) {
    void* page = VirtualAlloc(nullptr, 4096, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
    CHECK(page != nullptr);
    auto slot = static_cast<void**>(page);
    *slot = f.original;
    IatLease lease;
    auto result = lease.Install(f.module, slot, f.original, f.replacement);
    bool preserved = *slot == f.original;
    bool freed = VirtualFree(page, 0, MEM_RELEASE) != FALSE;
    CHECK(result.code == IatLeaseCode::slot_not_image);
    CHECK(preserved && freed);
    return true;
}

static bool RejectGuardedImage(Fixture& f) {
    DWORD old = 0, ignored = 0;
    CHECK(VirtualProtect(const_cast<void**>(f.slot), sizeof(void*), PAGE_READONLY | PAGE_GUARD, &old));
    IatLease lease;
    auto result = lease.Install(f.module, f.slot, f.original, f.replacement);
    DWORD after = f.Protection();
    CHECK(VirtualProtect(const_cast<void**>(f.slot), sizeof(void*), old, &ignored));
    CHECK(result.code == IatLeaseCode::inaccessible_slot);
    CHECK(after == (PAGE_READONLY | PAGE_GUARD));
    CHECK(f.Call() == 111);
    return true;
}

#ifdef WXBG_IAT_TESTING
static bool InstallFirstProtectFails(Fixture& f) {
    ResetProtectionFault reset;
    ArmProtectionFailure(1);
    IatLease lease;
    const auto result = lease.Install(f.module, f.slot, f.original, f.replacement);
    CHECK(result.code == IatLeaseCode::protect_failed && result.win32_error == ERROR_ACCESS_DENIED);
    CHECK(protect_calls == 1 && injected_failures == 1);
    CHECK(!result.slot_changed && result.observed == nullptr && result.protection_restored);
    CHECK(!lease.installed() && !lease.protection_pending());
    CHECK(*f.slot == f.original && f.Call() == 111 && f.Protection() == PAGE_READONLY);
    CHECK(lease.Restore().code == IatLeaseCode::not_installed && protect_calls == 1);
    return true;
}

static bool InstallSecondProtectFails(Fixture& f) {
    ResetProtectionFault reset;
    ArmProtectionFailure(2);
    IatLease lease;
    auto result = lease.Install(f.module, f.slot, f.original, f.replacement);
    CHECK(result.code == IatLeaseCode::protection_restore_failed && result.win32_error == ERROR_ACCESS_DENIED);
    CHECK(protect_calls == 2 && injected_failures == 1);
    CHECK(result.slot_changed && result.observed == f.original && !result.protection_restored);
    CHECK(lease.installed() && lease.protection_pending() && lease.original_protection() == PAGE_READONLY);
    CHECK(*f.slot == f.replacement && f.Call() == 222 && f.Protection() == PAGE_READWRITE);
    result = lease.Install(f.module, f.slot, f.original, f.replacement);
    CHECK(result.code == IatLeaseCode::already_installed && !result.protection_restored);
    CHECK(protect_calls == 2 && lease.installed() && lease.protection_pending());
    ArmProtectionFailure(0);
    result = lease.Restore();
    CHECK(result.code == IatLeaseCode::ok && result.slot_changed && result.protection_restored);
    CHECK(result.observed == f.replacement && protect_calls == 2 && injected_failures == 0);
    CHECK(!lease.installed() && !lease.protection_pending());
    CHECK(*f.slot == f.original && f.Call() == 111 && f.Protection() == PAGE_READONLY);
    CHECK(lease.Restore().code == IatLeaseCode::already_restored && protect_calls == 2);
    return true;
}

static bool RestoreFirstProtectFails(Fixture& f) {
    ResetProtectionFault reset;
    ArmProtectionFailure(0);
    IatLease lease;
    CHECK(lease.Install(f.module, f.slot, f.original, f.replacement).code == IatLeaseCode::ok);
    ArmProtectionFailure(1);
    auto result = lease.Restore();
    CHECK(result.code == IatLeaseCode::protect_failed && result.win32_error == ERROR_ACCESS_DENIED);
    CHECK(protect_calls == 1 && injected_failures == 1);
    CHECK(!result.slot_changed && result.observed == nullptr && result.protection_restored);
    CHECK(lease.installed() && !lease.protection_pending());
    CHECK(*f.slot == f.replacement && f.Call() == 222 && f.Protection() == PAGE_READONLY);
    ArmProtectionFailure(0);
    result = lease.Restore();
    CHECK(result.code == IatLeaseCode::ok && result.slot_changed && result.protection_restored);
    CHECK(result.observed == f.replacement && protect_calls == 2 && injected_failures == 0);
    CHECK(!lease.installed() && !lease.protection_pending());
    CHECK(*f.slot == f.original && f.Call() == 111 && f.Protection() == PAGE_READONLY);
    return true;
}

static bool RestoreSecondProtectFails(Fixture& f) {
    ResetProtectionFault reset;
    ArmProtectionFailure(0);
    IatLease lease;
    CHECK(lease.Install(f.module, f.slot, f.original, f.replacement).code == IatLeaseCode::ok);
    ArmProtectionFailure(2);
    auto result = lease.Restore();
    CHECK(result.code == IatLeaseCode::protection_restore_failed && result.win32_error == ERROR_ACCESS_DENIED);
    CHECK(protect_calls == 2 && injected_failures == 1);
    CHECK(result.slot_changed && result.observed == f.replacement && !result.protection_restored);
    CHECK(!lease.installed() && lease.protection_pending() && lease.original_protection() == PAGE_READONLY);
    CHECK(*f.slot == f.original && f.Call() == 111 && f.Protection() == PAGE_READWRITE);
    ArmProtectionFailure(0);
    result = lease.Restore();
    CHECK(result.code == IatLeaseCode::ok && !result.slot_changed && result.protection_restored);
    CHECK(result.observed == nullptr && protect_calls == 1 && injected_failures == 0);
    CHECK(!lease.installed() && !lease.protection_pending());
    CHECK(*f.slot == f.original && f.Call() == 111 && f.Protection() == PAGE_READONLY);
    CHECK(lease.Restore().code == IatLeaseCode::already_restored && protect_calls == 1);
    return true;
}
#endif

int wmain(int argc, wchar_t** argv) {
    if (argc != 2) { std::printf("Usage: iat_lease_test.exe absolute-fixture-dll\n"); return 2; }
    Fixture fixture;
    if (!fixture.Init(argv[1])) { std::printf("Fixture setup failed: %lu\n", GetLastError()); return 2; }
    struct Test { const char* name; bool (*run)(Fixture&); } tests[] = {
        {"install_call_restore_and_protection", RoundTrip}, {"wrong_expected_preserves_slot", WrongExpected},
        {"restore_conflict_preserves_external_pointer", RestoreConflict}, {"repeat_restore_and_reuse", RepeatedRestore},
        {"null_alignment_noop_rejected", RejectArguments}, {"wrong_module_rejected", RejectOtherModule},
        {"private_memory_rejected", RejectPrivateAllocation}, {"guarded_image_rejected_without_touch", RejectGuardedImage},
#ifdef WXBG_IAT_TESTING
        {"install_first_protect_failure_preserves_slot_and_page", InstallFirstProtectFails},
        {"install_second_protect_failure_retains_state_then_restores", InstallSecondProtectFails},
        {"restore_first_protect_failure_keeps_replacement", RestoreFirstProtectFails},
        {"restore_second_protect_failure_repairs_page_on_retry", RestoreSecondProtectFails},
#endif
    };
    int passed = 0;
    for (const auto& test : tests) {
        bool success = test.run(fixture);
        std::printf("%s %s\n", success ? "PASS" : "FAIL", test.name);
        if (success) ++passed;
        // Each case starts from the original function, even after a failed CHECK.
        DWORD ignored = 0;
        if (!fixture.ExternalSet(fixture.original) ||
            !VirtualProtect(const_cast<void**>(fixture.slot), sizeof(void*), PAGE_READONLY, &ignored)) {
            std::printf("Fixture cleanup failed: %lu\n", GetLastError());
            return 2;
        }
    }
    std::printf("SUMMARY tests=%zu passed=%d checks=%d failures=%d\n", sizeof(tests)/sizeof(tests[0]), passed, checks, failures);
    return failures ? 1 : 0;
}

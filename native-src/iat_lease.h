#pragma once
#ifndef _WIN32_WINNT
#define _WIN32_WINNT 0x0601
#endif
#include <windows.h>

namespace wxbg {
#ifdef WXBG_IAT_TESTING
// Link-time seam supplied only by the owned test executable. Production builds
// do not reference this function and contain no fault switch or test setter.
BOOL WINAPI IatTestVirtualProtect(LPVOID address, SIZE_T size, DWORD protection,
                                 PDWORD old_protection) noexcept;
#endif
enum class IatLeaseCode {
    ok, already_restored, not_installed, already_installed,
    invalid_argument, unaligned_slot, invalid_module, invalid_slot,
    slot_not_image, module_mismatch, outside_image, inaccessible_slot,
    protect_failed, compare_exchange_conflict, protection_restore_failed
};

struct IatLeaseResult {
    IatLeaseCode code;
    DWORD win32_error = ERROR_SUCCESS;
    void* observed = nullptr;
    bool slot_changed = false;
    // False means page protection still needs explicit repair. On a combined
    // CAS/protection failure, code prioritizes protection_restore_failed; inspect
    // observed/slot_changed and installed() as well.
    bool protection_restored = true;
};

// Same-process, explicit-lifetime data-pointer lease. This does not identify an
// IAT entry: the caller must already have verified the slot and both targets.
// Caller must keep module/targets loaded and serialize all page-protection
// changes affecting this slot's page, including other lease instances.
// No destructor restore: the caller must inspect Restore's result explicitly.
// A successful Restore NEVER proves callbacks/proxies or a DLL can be unloaded.
// Calls/getters on one instance require external serialization.
class IatLease final {
public:
    IatLease() noexcept = default;
    ~IatLease() = default;
    IatLease(const IatLease&) = delete;
    IatLease& operator=(const IatLease&) = delete;
    IatLease(IatLease&&) = delete;
    IatLease& operator=(IatLease&&) = delete;

    IatLeaseResult Install(HMODULE module, void* volatile* slot,
                           void* expected_original, void* replacement) noexcept;
    // Repeat after our successful restore is already_restored and never touches
    // the slot. Repeat after a CAS conflict retries only replacement -> original.
    // May also repair a prior protection failure with slot_changed == false.
    IatLeaseResult Restore() noexcept;
    // True means an installed lease has not been successfully restored. A CAS
    // conflict keeps this unresolved state; it does not claim current ownership.
    bool installed() const noexcept { return installed_; }
    bool protection_pending() const noexcept { return protection_pending_; }
    DWORD original_protection() const noexcept { return original_protection_; }

private:
    HMODULE module_ = nullptr;
    void* volatile* slot_ = nullptr;
    void* original_ = nullptr;
    void* replacement_ = nullptr;
    DWORD original_protection_ = 0;
    bool installed_ = false;
    bool restored_ = false;
    bool protection_pending_ = false;
};
} // namespace wxbg

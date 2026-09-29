#include "iat_lease.h"
#include <psapi.h>
#include <cstdint>

namespace wxbg {
namespace {
BOOL ProtectPage(LPVOID address, SIZE_T size, DWORD protection,
                 PDWORD old_protection) noexcept {
#ifdef WXBG_IAT_TESTING
    return IatTestVirtualProtect(address, size, protection, old_protection);
#else
    return VirtualProtect(address, size, protection, old_protection);
#endif
}

IatLeaseResult Validate(HMODULE module, void* volatile* slot,
                        MEMORY_BASIC_INFORMATION* memory) noexcept {
    if (!module || !slot) return {IatLeaseCode::invalid_argument};
    const auto address = reinterpret_cast<std::uintptr_t>(slot);
    if (address % alignof(void*) != 0) return {IatLeaseCode::unaligned_slot};
    if (VirtualQuery(const_cast<void**>(slot), memory, sizeof(*memory)) != sizeof(*memory))
        return {IatLeaseCode::invalid_slot, GetLastError()};
    if (memory->State != MEM_COMMIT || memory->Type != MEM_IMAGE)
        return {IatLeaseCode::slot_not_image};
    if (memory->AllocationBase != module) return {IatLeaseCode::module_mismatch};
    MODULEINFO image{};
    if (!GetModuleInformation(GetCurrentProcess(), module, &image, sizeof(image)))
        return {IatLeaseCode::invalid_module, GetLastError()};
    const auto base = reinterpret_cast<std::uintptr_t>(image.lpBaseOfDll);
    if (image.lpBaseOfDll != module || image.SizeOfImage < sizeof(void*) ||
        address < base || address - base > image.SizeOfImage - sizeof(void*))
        return {IatLeaseCode::outside_image};
    const auto region = reinterpret_cast<std::uintptr_t>(memory->BaseAddress);
    if (memory->RegionSize < sizeof(void*) || address < region ||
        address - region > memory->RegionSize - sizeof(void*))
        return {IatLeaseCode::invalid_slot};
    const DWORD access = memory->Protect & 0xff;
    if ((memory->Protect & PAGE_GUARD) ||
        (access != PAGE_READONLY && access != PAGE_READWRITE && access != PAGE_WRITECOPY &&
         access != PAGE_EXECUTE_READ && access != PAGE_EXECUTE_READWRITE &&
         access != PAGE_EXECUTE_WRITECOPY))
        return {IatLeaseCode::inaccessible_slot};
    return {IatLeaseCode::ok};
}

DWORD WritableProtection(DWORD original) noexcept {
    const DWORD access = original & 0xff;
    const bool executable = access == PAGE_EXECUTE_READ || access == PAGE_EXECUTE_READWRITE ||
                            access == PAGE_EXECUTE_WRITECOPY;
    return (executable ? PAGE_EXECUTE_READWRITE : PAGE_READWRITE) |
           (original & (PAGE_NOCACHE | PAGE_WRITECOMBINE));
}
} // namespace

IatLeaseResult IatLease::Install(HMODULE module, void* volatile* slot,
                               void* expected_original, void* replacement) noexcept {
    if (installed_ || protection_pending_)
        return {IatLeaseCode::already_installed, ERROR_SUCCESS, nullptr, false, !protection_pending_};
    if (!expected_original || !replacement || expected_original == replacement)
        return {IatLeaseCode::invalid_argument};
    MEMORY_BASIC_INFORMATION memory{};
    const auto validation = Validate(module, slot, &memory);
    if (validation.code != IatLeaseCode::ok) return validation;
    DWORD old_protection = 0;
    if (!ProtectPage(const_cast<void**>(slot), sizeof(void*),
                        WritableProtection(memory.Protect), &old_protection))
        return {IatLeaseCode::protect_failed, GetLastError()};

    module_ = module;
    slot_ = slot;
    original_ = expected_original;
    replacement_ = replacement;
    original_protection_ = old_protection;
    restored_ = false;
    protection_pending_ = true;
    void* observed = InterlockedCompareExchangePointer(slot_, replacement_, original_);
    installed_ = observed == original_;
    DWORD ignored = 0;
    if (!ProtectPage(const_cast<void**>(slot_), sizeof(void*), original_protection_, &ignored))
        return {IatLeaseCode::protection_restore_failed, GetLastError(), observed, installed_, false};
    protection_pending_ = false;
    return {installed_ ? IatLeaseCode::ok : IatLeaseCode::compare_exchange_conflict,
            ERROR_SUCCESS, observed, installed_, true};
}

IatLeaseResult IatLease::Restore() noexcept {
    if (!installed_ && !protection_pending_)
        return {restored_ ? IatLeaseCode::already_restored : IatLeaseCode::not_installed};
    MEMORY_BASIC_INFORMATION memory{};
    auto validation = Validate(module_, slot_, &memory);
    if (validation.code != IatLeaseCode::ok) {
        validation.protection_restored = !protection_pending_;
        return validation;
    }

    // Repair a failed protection restore even if the earlier CAS never installed
    // our pointer, or successfully removed it before VirtualProtect failed.
    if (!installed_) {
        DWORD ignored = 0;
        if (!ProtectPage(const_cast<void**>(slot_), sizeof(void*), original_protection_, &ignored))
            return {IatLeaseCode::protection_restore_failed, GetLastError(), nullptr, false, false};
        protection_pending_ = false;
        return {IatLeaseCode::ok};
    }

    DWORD old_protection = 0;
    if (!ProtectPage(const_cast<void**>(slot_), sizeof(void*),
                        WritableProtection(memory.Protect), &old_protection))
        return {IatLeaseCode::protect_failed, GetLastError(), nullptr, false, !protection_pending_};
    protection_pending_ = true;
    void* observed = InterlockedCompareExchangePointer(slot_, original_, replacement_);
    const bool changed = observed == replacement_;
    if (changed) {
        installed_ = false;
        restored_ = true;
    }
    DWORD ignored = 0;
    if (!ProtectPage(const_cast<void**>(slot_), sizeof(void*), original_protection_, &ignored))
        return {IatLeaseCode::protection_restore_failed, GetLastError(), observed, changed, false};
    protection_pending_ = false;
    return {changed ? IatLeaseCode::ok : IatLeaseCode::compare_exchange_conflict,
            ERROR_SUCCESS, observed, changed, true};
}
} // namespace wxbg

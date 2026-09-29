#pragma once
#ifndef _WIN32_WINNT
#define _WIN32_WINNT 0x0601
#endif
#include <windows.h>
#include <cstdint>

namespace wxbg {
// Owns a read handle which allows other readers but denies new writers/deleters.
// Caller serializes access. No COM/UI/process operations. Failure leaves an
// unopened grant invalid; opening an already valid grant preserves it and fails.
class FixtureGrant final {
public:
    static constexpr std::uint64_t kMaxBytes = 1024 * 1024;
    static constexpr DWORD kPathCapacity = 260; // Includes the terminating NUL.
    FixtureGrant() noexcept = default;
    ~FixtureGrant() { Close(); }
    FixtureGrant(const FixtureGrant&) = delete;
    FixtureGrant& operator=(const FixtureGrant&) = delete;
    FixtureGrant(FixtureGrant&&) = delete;
    FixtureGrant& operator=(FixtureGrant&&) = delete;

    HRESULT Open(const wchar_t* absolute_path, std::uint64_t expected_size,
                 const unsigned char expected_sha256[32]) noexcept;
    const wchar_t* path() const noexcept { return path_; }
    bool valid() const noexcept { return handle_ != INVALID_HANDLE_VALUE; }
    void Close() noexcept;

private:
    HANDLE handle_ = INVALID_HANDLE_VALUE;
    wchar_t path_[kPathCapacity]{};
};
} // namespace wxbg

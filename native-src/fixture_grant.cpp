#include "fixture_grant.h"
#include <bcrypt.h>
#include <cwchar>

namespace wxbg {
namespace {
struct FileHandle {
    HANDLE value = INVALID_HANDLE_VALUE;
    ~FileHandle() { if (value != INVALID_HANDLE_VALUE) CloseHandle(value); }
};
struct HashHandles {
    BCRYPT_ALG_HANDLE algorithm = nullptr;
    BCRYPT_HASH_HANDLE hash = nullptr;
    ~HashHandles() {
        if (hash) BCryptDestroyHash(hash);
        if (algorithm) BCryptCloseAlgorithmProvider(algorithm, 0);
    }
};

bool LocalDrivePath(const wchar_t* path) noexcept {
    const bool letter = (path[0] >= L'A' && path[0] <= L'Z') ||
                        (path[0] >= L'a' && path[0] <= L'z');
    if (!letter || path[1] != L':' || path[2] != L'\\') return false;
    wchar_t root[] = {path[0], L':', L'\\', 0};
    const UINT type = GetDriveTypeW(root);
    return type == DRIVE_FIXED || type == DRIVE_REMOVABLE || type == DRIVE_RAMDISK;
}

HRESULT HashFile(HANDLE file, std::uint64_t size, const unsigned char expected[32]) noexcept {
    HashHandles handles;
    NTSTATUS status = BCryptOpenAlgorithmProvider(&handles.algorithm, BCRYPT_SHA256_ALGORITHM, nullptr, 0);
    if (status < 0) return HRESULT_FROM_NT(status);
    // Windows 7+ owns the hash object storage when pbHashObject is nullptr.
    status = BCryptCreateHash(handles.algorithm, &handles.hash, nullptr, 0, nullptr, 0, 0);
    if (status < 0) return HRESULT_FROM_NT(status);
    unsigned char buffer[64 * 1024];
    std::uint64_t total = 0;
    while (total < size) {
        const auto remaining = size - total;
        const DWORD requested = static_cast<DWORD>(remaining < sizeof(buffer) ? remaining : sizeof(buffer));
        DWORD read = 0;
        if (!ReadFile(file, buffer, requested, &read, nullptr)) return HRESULT_FROM_WIN32(GetLastError());
        if (!read || read > requested) return HRESULT_FROM_WIN32(ERROR_BAD_LENGTH);
        status = BCryptHashData(handles.hash, buffer, read, 0);
        if (status < 0) return HRESULT_FROM_NT(status);
        total += read;
    }
    unsigned char actual[32]{};
    status = BCryptFinishHash(handles.hash, actual, sizeof(actual), 0);
    if (status < 0) return HRESULT_FROM_NT(status);
    unsigned char difference = 0;
    for (unsigned i = 0; i < sizeof(actual); ++i) difference |= actual[i] ^ expected[i];
    return difference ? HRESULT_FROM_WIN32(ERROR_CRC) : S_OK;
}
} // namespace

HRESULT FixtureGrant::Open(const wchar_t* absolute_path, std::uint64_t expected_size,
                           const unsigned char expected_sha256[32]) noexcept {
    if (valid()) return HRESULT_FROM_WIN32(ERROR_ALREADY_INITIALIZED);
    if (!absolute_path || !expected_sha256) return E_INVALIDARG;
    if (expected_size > kMaxBytes) return HRESULT_FROM_WIN32(ERROR_FILE_TOO_LARGE);
    DWORD length = 0;
    while (length < kPathCapacity && absolute_path[length]) ++length;
    if (length == kPathCapacity) return HRESULT_FROM_WIN32(ERROR_FILENAME_EXCED_RANGE);
    if (length < 4 || !LocalDrivePath(absolute_path) || wcspbrk(absolute_path + 2, L":*?\"<>|"))
        return E_INVALIDARG;

    FileHandle file;
    file.value = CreateFileW(absolute_path, GENERIC_READ, FILE_SHARE_READ, nullptr,
                             OPEN_EXISTING, FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_SEQUENTIAL_SCAN, nullptr);
    if (file.value == INVALID_HANDLE_VALUE) return HRESULT_FROM_WIN32(GetLastError());
    if (GetFileType(file.value) != FILE_TYPE_DISK) return E_INVALIDARG;
    BY_HANDLE_FILE_INFORMATION information{};
    if (!GetFileInformationByHandle(file.value, &information)) return HRESULT_FROM_WIN32(GetLastError());
    if (information.dwFileAttributes & (FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT))
        return E_INVALIDARG;
    const auto actual_size = (static_cast<std::uint64_t>(information.nFileSizeHigh) << 32) |
                              information.nFileSizeLow;
    if (actual_size > kMaxBytes) return HRESULT_FROM_WIN32(ERROR_FILE_TOO_LARGE);
    if (actual_size != expected_size) return HRESULT_FROM_WIN32(ERROR_BAD_LENGTH);

    // A drive-letter path can traverse a junction. Check the opened object's
    // final DOS path too, rejecting a final UNC/network/device destination.
    wchar_t final_path[kPathCapacity + 4]{};
    const DWORD final_length = GetFinalPathNameByHandleW(file.value, final_path,
                                                        kPathCapacity + 4, FILE_NAME_NORMALIZED | VOLUME_NAME_DOS);
    if (!final_length) return HRESULT_FROM_WIN32(GetLastError());
    if (final_length >= kPathCapacity + 4) return HRESULT_FROM_WIN32(ERROR_FILENAME_EXCED_RANGE);
    if (final_length < 8 || wcsncmp(final_path, L"\\\\?\\", 4) != 0 || !LocalDrivePath(final_path + 4))
        return E_INVALIDARG;
    const DWORD stored_length = final_length - 4;
    if (stored_length >= kPathCapacity) return HRESULT_FROM_WIN32(ERROR_FILENAME_EXCED_RANGE);

    const HRESULT hashed = HashFile(file.value, expected_size, expected_sha256);
    if (FAILED(hashed)) return hashed;
    LARGE_INTEGER final_size{};
    if (!GetFileSizeEx(file.value, &final_size)) return HRESULT_FROM_WIN32(GetLastError());
    if (static_cast<std::uint64_t>(final_size.QuadPart) != expected_size)
        return HRESULT_FROM_WIN32(ERROR_BAD_LENGTH);
    // Publish only after every check succeeds. The same handle remains open.
    for (DWORD i = 0; i <= stored_length; ++i) path_[i] = final_path[i + 4];
    handle_ = file.value;
    file.value = INVALID_HANDLE_VALUE;
    return S_OK;
}

void FixtureGrant::Close() noexcept {
    const HANDLE old = handle_;
    handle_ = INVALID_HANDLE_VALUE;
    path_[0] = 0;
    if (old != INVALID_HANDLE_VALUE) CloseHandle(old);
}
} // namespace wxbg

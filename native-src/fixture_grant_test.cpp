#include "fixture_grant.h"
#include <cstdio>
#include <cwchar>
#include <string>

using wxbg::FixtureGrant;
static int checks = 0, failures = 0;
#define CHECK(x) do { ++checks; if (!(x)) { ++failures; std::printf("FAIL line %d: %s\n", __LINE__, #x); return false; } } while (0)
// Independent known SHA-256 vector for exactly three bytes: ASCII "abc".
static constexpr unsigned char abc_sha256[32] = {
    0xba,0x78,0x16,0xbf,0x8f,0x01,0xcf,0xea,0x41,0x41,0x40,0xde,0x5d,0xae,0x22,0x23,
    0xb0,0x03,0x61,0xa3,0x96,0x17,0x7a,0x9c,0xb4,0x10,0xff,0x61,0xf2,0x00,0x15,0xad
};
struct Fixture {
    std::wstring root, file, missing, large, link, stream;
    bool Init() {
        wchar_t executable[32768];
        DWORD n = GetModuleFileNameW(nullptr, executable, 32768);
        if (!n || n >= 32768) return false;
        root.assign(executable, n);
        root.resize(root.find_last_of(L"\\/") + 1);
        root += L"fixture-grant-owned-" + std::to_wstring(GetCurrentProcessId());
        if (!CreateDirectoryW(root.c_str(), nullptr)) return false;
        file = root + L"\\abc.bin";
        missing = root + L"\\not-created.bin";
        large = root + L"\\too-large.bin";
        link = root + L"\\leaf-link.bin";
        stream = root + L"\\stream-boundary.bin";
        HANDLE h = CreateFileW(file.c_str(), GENERIC_WRITE, 0, nullptr, CREATE_NEW, FILE_ATTRIBUTE_NORMAL, nullptr);
        if (h == INVALID_HANDLE_VALUE) return false;
        DWORD written = 0;
        bool ok = WriteFile(h, "abc", 3, &written, nullptr) && written == 3;
        CloseHandle(h);
        return ok;
    }
    bool CanWrite() const {
        HANDLE h = CreateFileW(file.c_str(), GENERIC_WRITE, FILE_SHARE_READ, nullptr, OPEN_EXISTING, 0, nullptr);
        if (h == INVALID_HANDLE_VALUE) return false;
        return CloseHandle(h) != FALSE;
    }
    bool Cleanup() {
        for (const auto* p : {&link, &large, &stream, &file}) {
            if (!p->empty() && !DeleteFileW(p->c_str()) && GetLastError() != ERROR_FILE_NOT_FOUND) return false;
        }
        return RemoveDirectoryW(root.c_str()) != FALSE;
    }
};

static bool CorrectFile(Fixture& f) {
    FixtureGrant grant;
    CHECK(!grant.valid() && grant.path()[0] == 0);
    CHECK(grant.Open(f.file.c_str(), 3, abc_sha256) == S_OK);
    CHECK(grant.valid() && _wcsicmp(grant.path(), f.file.c_str()) == 0);
    CHECK(grant.Open(f.file.c_str(), 3, abc_sha256) == HRESULT_FROM_WIN32(ERROR_ALREADY_INITIALIZED));
    CHECK(grant.valid() && _wcsicmp(grant.path(), f.file.c_str()) == 0);
    grant.Close();
    CHECK(!grant.valid() && grant.path()[0] == 0 && f.CanWrite());
    grant.Close();
    CHECK(!grant.valid() && grant.path()[0] == 0);
    return true;
}

static bool WrongSizeAndHash(Fixture& f) {
    FixtureGrant grant;
    CHECK(grant.Open(f.file.c_str(), 4, abc_sha256) == HRESULT_FROM_WIN32(ERROR_BAD_LENGTH));
    CHECK(!grant.valid() && grant.path()[0] == 0 && f.CanWrite());
    unsigned char wrong[32]{};
    CHECK(grant.Open(f.file.c_str(), 3, wrong) == HRESULT_FROM_WIN32(ERROR_CRC));
    CHECK(!grant.valid() && grant.path()[0] == 0 && f.CanWrite());
    CHECK(grant.Open(f.file.c_str(), 3, abc_sha256) == S_OK);
    return true;
}

static bool BadPaths(Fixture& f) {
    FixtureGrant grant;
    CHECK(FAILED(grant.Open(f.root.c_str(), 3, abc_sha256)));
    CHECK(!grant.valid() && grant.path()[0] == 0);
    CHECK(grant.Open(f.missing.c_str(), 3, abc_sha256) == HRESULT_FROM_WIN32(ERROR_FILE_NOT_FOUND));
    CHECK(!grant.valid() && grant.path()[0] == 0);
    for (const wchar_t* path : {L"abc.bin", L"C:abc.bin", L"\\\\server\\share\\file", L"\\\\?\\C:\\file", L"\\\\.\\NUL", L"C:\\file:stream", L"C:\\*.bin"}) {
        CHECK(grant.Open(path, 3, abc_sha256) == E_INVALIDARG);
        CHECK(!grant.valid() && grant.path()[0] == 0);
    }
    CHECK(grant.Open(nullptr, 3, abc_sha256) == E_INVALIDARG);
    CHECK(grant.Open(f.file.c_str(), 3, nullptr) == E_INVALIDARG);
    std::wstring too_long = L"C:\\" + std::wstring(260, L'a');
    CHECK(grant.Open(too_long.c_str(), 3, abc_sha256) == HRESULT_FROM_WIN32(ERROR_FILENAME_EXCED_RANGE));
    CHECK(f.CanWrite());
    return true;
}

static bool BoundedSize(Fixture& f) {
    FixtureGrant grant;
    CHECK(grant.Open(f.file.c_str(), FixtureGrant::kMaxBytes + 1, abc_sha256) == HRESULT_FROM_WIN32(ERROR_FILE_TOO_LARGE));
    CHECK(!grant.valid());
    HANDLE h = CreateFileW(f.large.c_str(), GENERIC_WRITE, 0, nullptr, CREATE_NEW, 0, nullptr);
    CHECK(h != INVALID_HANDLE_VALUE);
    LARGE_INTEGER length{}; length.QuadPart = FixtureGrant::kMaxBytes + 1;
    bool created = SetFilePointerEx(h, length, nullptr, FILE_BEGIN) && SetEndOfFile(h);
    CloseHandle(h);
    CHECK(created);
    CHECK(grant.Open(f.large.c_str(), 3, abc_sha256) == HRESULT_FROM_WIN32(ERROR_FILE_TOO_LARGE));
    CHECK(!grant.valid() && grant.path()[0] == 0);
    CHECK(DeleteFileW(f.large.c_str()));
    return true;
}

static bool SharingAndClose(Fixture& f) {
    FixtureGrant grant;
    CHECK(grant.Open(f.file.c_str(), 3, abc_sha256) == S_OK);
    HANDLE reader = CreateFileW(f.file.c_str(), GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING, 0, nullptr);
    CHECK(reader != INVALID_HANDLE_VALUE);
    CloseHandle(reader);
    HANDLE writer = CreateFileW(f.file.c_str(), GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, nullptr, OPEN_EXISTING, 0, nullptr);
    DWORD write_error = GetLastError();
    if (writer != INVALID_HANDLE_VALUE) CloseHandle(writer);
    CHECK(writer == INVALID_HANDLE_VALUE && write_error == ERROR_SHARING_VIOLATION);
    BOOL deleted = DeleteFileW(f.file.c_str());
    DWORD delete_error = GetLastError();
    CHECK(!deleted && delete_error == ERROR_SHARING_VIOLATION);
    HANDLE deleter = CreateFileW(f.file.c_str(), DELETE, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, nullptr, OPEN_EXISTING, 0, nullptr);
    DWORD access_error = GetLastError();
    if (deleter != INVALID_HANDLE_VALUE) CloseHandle(deleter);
    CHECK(deleter == INVALID_HANDLE_VALUE && access_error == ERROR_SHARING_VIOLATION);
    grant.Close();
    CHECK(!grant.valid() && grant.path()[0] == 0);
    writer = CreateFileW(f.file.c_str(), GENERIC_WRITE, FILE_SHARE_READ, nullptr, OPEN_EXISTING, 0, nullptr);
    CHECK(writer != INVALID_HANDLE_VALUE);
    DWORD written = 0;
    bool wrote = WriteFile(writer, "abc", 3, &written, nullptr) && written == 3;
    CloseHandle(writer);
    CHECK(wrote);
    CHECK(DeleteFileW(f.file.c_str()));
    return true;
}

static bool DestructorCloses(Fixture& f) {
    {
        FixtureGrant grant;
        CHECK(grant.Open(f.file.c_str(), 3, abc_sha256) == S_OK);
        CHECK(!f.CanWrite());
    }
    CHECK(f.CanWrite());
    return true;
}

static bool LeafReparseRejected(Fixture& f) {
    // Owned link only; OPEN_REPARSE_POINT must reject the link rather than hash
    // and accept its otherwise-valid target. No privilege settings are changed.
    BOOL created = CreateSymbolicLinkW(f.link.c_str(), f.file.c_str(), SYMBOLIC_LINK_FLAG_ALLOW_UNPRIVILEGED_CREATE);
    if (!created && GetLastError() == ERROR_INVALID_PARAMETER)
        created = CreateSymbolicLinkW(f.link.c_str(), f.file.c_str(), 0);
    CHECK(created);
    CHECK(GetFileAttributesW(f.link.c_str()) & FILE_ATTRIBUTE_REPARSE_POINT);
    FixtureGrant grant;
    CHECK(grant.Open(f.link.c_str(), 3, abc_sha256) == E_INVALIDARG);
    CHECK(!grant.valid() && grant.path()[0] == 0 && f.CanWrite());
    CHECK(DeleteFileW(f.link.c_str()));
    CHECK(GetFileAttributesW(f.file.c_str()) != INVALID_FILE_ATTRIBUTES);
    return true;
}

static bool StreamingBoundary(Fixture& f) {
    // SHA-256 of 65,537 ASCII 'a' bytes, cross-checked with .NET SHA256.HashData.
    static constexpr unsigned char expected[32] = {
        0x00,0x8f,0xfc,0x88,0xd3,0xc9,0x6a,0x9f,0x30,0x75,0x24,0xeb,0x36,0x1e,0x47,0xc5,
        0x22,0x2a,0x88,0x7f,0xc4,0x5f,0xa0,0xc1,0xfb,0x8d,0x42,0x9c,0x5c,0x23,0xb4,0x30
    };
    HANDLE h = CreateFileW(f.stream.c_str(), GENERIC_WRITE, 0, nullptr, CREATE_NEW, 0, nullptr);
    CHECK(h != INVALID_HANDLE_VALUE);
    char buffer[4096];
    for (auto& byte : buffer) byte = 'a';
    DWORD remaining = 65537;
    bool written_ok = true;
    while (remaining) {
        DWORD count = remaining < sizeof(buffer) ? remaining : sizeof(buffer);
        DWORD written = 0;
        if (!WriteFile(h, buffer, count, &written, nullptr) || written != count) { written_ok = false; break; }
        remaining -= written;
    }
    CloseHandle(h);
    CHECK(written_ok);
    FixtureGrant grant;
    CHECK(grant.Open(f.stream.c_str(), 65537, expected) == S_OK);
    CHECK(grant.valid() && _wcsicmp(grant.path(), f.stream.c_str()) == 0);
    grant.Close();
    CHECK(DeleteFileW(f.stream.c_str()));
    return true;
}

int main() {
    Fixture fixture;
    if (!fixture.Init()) { std::printf("Owned fixture setup failed: %lu\n", GetLastError()); return 2; }
    struct Test { const char* name; bool (*run)(Fixture&); } tests[] = {
        {"correct_sha256_size_path_and_repeated_close", CorrectFile},
        {"wrong_size_hash_fail_without_lock_leak", WrongSizeAndHash},
        {"directory_missing_unsafe_and_long_paths", BadPaths},
        {"one_megabyte_bound", BoundedSize},
        {"leaf_reparse_rejected_without_target_lock", LeafReparseRejected},
        {"sha256_stream_crosses_64KiB_boundary", StreamingBoundary},
        {"destructor_releases_read_handle", DestructorCloses},
        {"read_allowed_write_delete_denied_until_close", SharingAndClose}
    };
    int passed = 0;
    for (const auto& test : tests) {
        const bool ok = test.run(fixture);
        if (ok) ++passed;
        std::printf("%s %s\n", ok ? "PASS" : "FAIL", test.name);
    }
    const bool cleaned = fixture.Cleanup();
    std::printf("SUMMARY tests=%zu passed=%d checks=%d failures=%d fixture_cleanup=%s\n",
                sizeof(tests)/sizeof(tests[0]), passed, checks, failures, cleaned ? "true" : "false");
    return failures || !cleaned ? 1 : 0;
}

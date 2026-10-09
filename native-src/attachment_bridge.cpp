#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <shobjidl.h>
#include <stdint.h>
#include <stdio.h>
#include "iat_lease.h"
#include "file_dialog_proxy.h"
#include "fixture_grant.h"

// Experimental cancel-only attachment routing. No arbitrary addresses/calls.
// Once an IAT lease is installed the module is intentionally resident until
// process exit: restoring a pointer is not a proof of callback quiescence.
constexpr uint32_t MAGIC=0x57424134;
constexpr wchar_t MESSAGE[]=L"WxBg.AttachmentProbe.v4.8AB0B35C-A2EF-4F7C-ADDE-77A62178ECA7";
struct Data {
    uint32_t magic,version,size,command;
    uint64_t nonce,request_deadline;
    uint32_t pid,tid,kind,x,y,client_width;
    volatile LONG state;
    uint32_t error,installed,lease_code,protection_restored,matches,shows,live,resident,timer_expired;
    uint32_t restore_attempts,cleanup_unresolved;
    uint32_t active_filter,cleanup_exhausted;
    uint32_t accepted_shows,selection_mode,grant_revoked,client_height;
    uint64_t module,slot,current,original,replacement,lease_deadline;
    uint64_t expected_size;
    wchar_t fixture_path[260];
    unsigned char fixture_sha256[32];
};
using CreateFn=HRESULT(WINAPI *)(REFCLSID,LPUNKNOWN,DWORD,REFIID,LPVOID *);
static CreateFn original=nullptr;
static wxbg::IatLease lease;
static HMODULE image=nullptr,resident_module=nullptr;
static void *volatile *slot=nullptr;
static DWORD ui_tid=0;
static UINT_PTR timer=0;
static volatile LONG busy=0,active=0,matches=0;
static ULONGLONG lease_deadline=0;
static LONG shows_before=0;
static LONG accepted_before=0;
static uint32_t selection_mode=0;
static wxbg::FixtureGrant fixture_grant;
struct PositiveGrantState { volatile LONG allowed=0; alignas(8) volatile LONG64 generation=0; };
static PositiveGrantState positive_grant;
static BOOL WINAPI AuthorizePositive(void *context,ULONGLONG generation) {
    return context==&positive_grant &&
        InterlockedCompareExchange(&positive_grant.allowed,0,0)!=0 &&
        static_cast<ULONGLONG>(InterlockedCompareExchange64(&positive_grant.generation,0,0))==generation;
}
static wxbg::IatLeaseResult last_result{wxbg::IatLeaseCode::not_installed};
static bool timer_expired=false;
static uint32_t restore_attempts=0;
static bool cleanup_unresolved=false;

static HRESULT WINAPI InterceptCreate(REFCLSID clsid,LPUNKNOWN outer,DWORD context,REFIID iid,LPVOID *out) {
    CreateFn invoke=original;
    if (!invoke) return E_UNEXPECTED;
    if (InterlockedCompareExchange(&active,0,0) && GetCurrentThreadId()==ui_tid &&
        IsEqualCLSID(clsid,CLSID_FileOpenDialog)) {
        if (!out) return E_POINTER;
        *out=nullptr;
        // Return a valid cancelling object even for duplicate/late activations
        // while armed. Failing the factory may make Qt open its widget picker.
        // Multiple activations fail acceptance; they never forward into real Show.
        if (outer || context!=CLSCTX_INPROC_SERVER || !IsEqualIID(iid,IID_IFileOpenDialog))
            return HRESULT_FROM_WIN32(ERROR_CANCELLED);
        InterlockedIncrement(&matches);
        IFileOpenDialog *real=nullptr;
        HRESULT hr=invoke(clsid,outer,context,iid,reinterpret_cast<void **>(&real));
        if (FAILED(hr) || !real) return FAILED(hr)?hr:E_UNEXPECTED;
        IFileOpenDialog *proxy=nullptr;
        // Unexpected duplicate activations are always cancelled, never given a
        // second copy of the file. Acceptance checks the exact activation count.
        ULONGLONG generation=static_cast<ULONGLONG>(InterlockedCompareExchange64(&positive_grant.generation,0,0));
        const wchar_t *path=(selection_mode==1 && matches==1 && fixture_grant.valid() &&
            GetTickCount64()<lease_deadline && AuthorizePositive(&positive_grant,generation))?fixture_grant.path():nullptr;
        WxBgFileProxyGrant grant={generation,lease_deadline,AuthorizePositive,&positive_grant};
        hr=CreateWxBgFileOpenProxyWithGrant(real,path,path?&grant:nullptr,&proxy);
        real->Release();
        if (FAILED(hr)) return hr;
        *out=proxy;
        return S_OK;
    }
    return invoke(clsid,outer,context,iid,out);
}

static void Restore() {
    // Revoke before attempting pointer cleanup. Existing proxies carry this
    // generation and check it before Show/callback/results/final acceptance.
    InterlockedExchange(&positive_grant.allowed,0);
    ++restore_attempts;
    if (lease.installed() || lease.protection_pending()) last_result=lease.Restore();
    cleanup_unresolved=lease.installed() || lease.protection_pending();
    if (!cleanup_unresolved) {
        InterlockedExchange(&active,0);
        fixture_grant.Close();
        if(timer){UINT_PTR old=timer;timer=0;KillTimer(nullptr,old);}
    } else if(restore_attempts>=3) {
        // Keep the module resident, but stop cancelling unrelated future opens.
        // If our slot remains installed it becomes a transparent original call.
        // A later explicit status/recovery command can inspect and retry safely.
        InterlockedExchange(&active,0);
        fixture_grant.Close();
        if(timer){UINT_PTR old=timer;timer=0;KillTimer(nullptr,old);}
    }
}
static VOID CALLBACK Expire(HWND,UINT,UINT_PTR callback_timer, DWORD) {
    // KillTimer does not remove queued messages. A stale callback must never
    // restore a newer operation, even if Windows recycles a timer identifier.
    if(callback_timer!=timer || GetTickCount64()<lease_deadline) return;
    timer_expired=true;
    Restore();
}
static DWORD Resolve(uint32_t kind) {
    if (kind>1) return ERROR_INVALID_PARAMETER;
    HMODULE candidate=GetModuleHandleW(kind?L"Weixin.dll":L"attachment_fixture.dll");
    if (!candidate) return ERROR_MOD_NOT_FOUND;
    auto *base=reinterpret_cast<unsigned char *>(candidate);
    auto *dos=reinterpret_cast<IMAGE_DOS_HEADER *>(base);
    if (dos->e_magic!=IMAGE_DOS_SIGNATURE || dos->e_lfanew<=0 || dos->e_lfanew>0x1000) return ERROR_BAD_EXE_FORMAT;
    auto *nt=reinterpret_cast<IMAGE_NT_HEADERS64 *>(base+dos->e_lfanew);
    if (nt->Signature!=IMAGE_NT_SIGNATURE || nt->OptionalHeader.Magic!=IMAGE_NT_OPTIONAL_HDR64_MAGIC) return ERROR_BAD_EXE_FORMAT;
    void *volatile *candidate_slot=nullptr;
    if (kind) {
        constexpr unsigned char signature[]={0x48,0x85,0xc9,0x0f,0x84,0x55,0x06,0x00,0x00,0x80,0x3d,0x76,0xf1,0x4e,0x0a,0x00,0x0f,0x84,0x48,0x06,0x00,0x00};
        if (nt->OptionalHeader.SizeOfImage!=0xbc2e000 || memcmp(base+0x82a4e2,signature,20)) return ERROR_REVISION_MISMATCH;
        candidate_slot=reinterpret_cast<void *volatile *>(base+0xb4b6ae0);
    } else {
        using SlotFn=void *volatile *(*)();
        auto get_slot=reinterpret_cast<SlotFn>(GetProcAddress(candidate,"AttachmentFixtureSlot"));
        if (!get_slot) return ERROR_PROC_NOT_FOUND;
        candidate_slot=get_slot();
    }
    HMODULE ole=GetModuleHandleW(L"ole32.dll");
    if (!ole) return ERROR_MOD_NOT_FOUND;
    auto fn=reinterpret_cast<CreateFn>(GetProcAddress(ole,"CoCreateInstance"));
    if (!fn) return ERROR_PROC_NOT_FOUND;
    if (original && original!=fn) return ERROR_INVALID_FUNCTION;
    image=candidate;slot=candidate_slot;
    if (!original) original=fn; // Immutable before the first IAT publication.
    return ERROR_SUCCESS;
}
static void Snapshot(Data *data) {
    data->installed=lease.installed();data->lease_code=static_cast<uint32_t>(last_result.code);
    data->protection_restored=last_result.protection_restored;
    data->matches=static_cast<uint32_t>(InterlockedCompareExchange(&matches,0,0));
    data->shows=static_cast<uint32_t>(WxBgFileOpenProxyCancelledShowCount()-shows_before);
    data->accepted_shows=static_cast<uint32_t>(WxBgFileOpenProxyAcceptedShowCount()-accepted_before);
    data->selection_mode=selection_mode;
    data->grant_revoked=InterlockedCompareExchange(&positive_grant.allowed,0,0)==0;
    data->live=static_cast<uint32_t>(WxBgFileOpenProxyLiveCount());
    data->resident=resident_module!=nullptr;data->timer_expired=timer_expired;
    data->restore_attempts=restore_attempts;data->cleanup_unresolved=cleanup_unresolved;
    data->active_filter=InterlockedCompareExchange(&active,0,0)!=0;
    data->cleanup_exhausted=cleanup_unresolved && restore_attempts>=3;
    data->module=reinterpret_cast<uint64_t>(image);data->slot=reinterpret_cast<uint64_t>(slot);
    data->current=slot?reinterpret_cast<uint64_t>(*slot):0;
    data->original=reinterpret_cast<uint64_t>(original);data->replacement=reinterpret_cast<uint64_t>(&InterceptCreate);
    data->lease_deadline=lease_deadline;
}

struct RenderLayout {
    HWND hwnd=nullptr;
    unsigned count=0;
};
static BOOL CALLBACK CollectRender(HWND child,LPARAM context) {
    auto *layout=reinterpret_cast<RenderLayout *>(context);
    wchar_t class_name[64]={};
    if (GetClassNameW(child,class_name,64) &&
        (lstrcmpW(class_name,L"MMUIRenderSubWindowHW")==0 ||
         lstrcmpW(class_name,L"MMUIRenderSubWindow")==0)) {
        ++layout->count;
        if (layout->count==1) layout->hwnd=child;
    }
    return TRUE;
}
static bool ValidProductionAttachmentLayout(HWND hwnd,uint32_t x,uint32_t y,
                                            uint32_t width,uint32_t height) {
    if (width<640 || width>32767 || height<480 || height>32767 ||
        x<8 || x>width-8 || y<8 || y>height-8 ||
        x<16 || x>width*3/4 || y<height*3/4 ||
        GetAncestor(hwnd,GA_ROOT)!=hwnd) return false;
    wchar_t main_class[64]={};
    if (!GetClassNameW(hwnd,main_class,64) ||
        lstrcmpW(main_class,L"Qt51514QWindowIcon")!=0) return false;
    RenderLayout layout{};
    if (!EnumChildWindows(hwnd,CollectRender,reinterpret_cast<LPARAM>(&layout)) ||
        layout.count!=1 || !layout.hwnd) return false;
    DWORD root_pid=0,render_pid=0;
    GetWindowThreadProcessId(hwnd,&root_pid);
    GetWindowThreadProcessId(layout.hwnd,&render_pid);
    if (!root_pid || root_pid!=GetCurrentProcessId() || render_pid!=root_pid) return false;
    RECT root_client{},render_client{};
    if (!GetClientRect(hwnd,&root_client) || !GetClientRect(layout.hwnd,&render_client) ||
        root_client.left!=0 || root_client.top!=0 ||
        render_client.left!=0 || render_client.top!=0 ||
        (!IsIconic(hwnd) &&
         (root_client.right!=static_cast<LONG>(width) ||
          root_client.bottom!=static_cast<LONG>(height))) ||
        render_client.right!=static_cast<LONG>(width) ||
        render_client.bottom!=static_cast<LONG>(height)) return false;
    POINT child_origin{0,0};
    return ClientToScreen(layout.hwnd,&child_origin) &&
           ScreenToClient(hwnd,&child_origin) &&
           child_origin.x==0 && child_origin.y==0;
}
static DWORD Arm(Data *data,HWND hwnd) {
    if (lease.installed() || lease.protection_pending() || WxBgFileOpenProxyLiveCount()) return ERROR_BUSY;
    DWORD err=Resolve(data->kind);if(err)return err;
    if (*slot!=reinterpret_cast<void *>(original)) return ERROR_INVALID_FUNCTION;
    if (data->kind && (!IsIconic(hwnd) ||
        !ValidProductionAttachmentLayout(hwnd,data->x,data->y,
                                         data->client_width,data->client_height) ||
        reinterpret_cast<unsigned char *>(image)[0xad19668]!=1)) return ERROR_INVALID_STATE;
    if (!data->kind && (data->x!=12 || data->y!=12)) return ERROR_INVALID_PARAMETER;
    if ((GetKeyState(VK_SHIFT)|GetKeyState(VK_CONTROL)|GetKeyState(VK_MENU)) & 0x8000) return ERROR_BUSY;
    GUITHREADINFO info={};info.cbSize=sizeof(info);
    if (!GetGUIThreadInfo(0,&info) || info.hwndCapture) return ERROR_BUSY;
    if(data->command==5) {
        if(wcsnlen(data->fixture_path,260)>=260) return ERROR_INVALID_PARAMETER;
        HRESULT opened=fixture_grant.Open(data->fixture_path,data->expected_size,data->fixture_sha256);
        if(FAILED(opened)) return ERROR_INVALID_DATA;
    } else fixture_grant.Close();
    if (!resident_module && !GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS,
        reinterpret_cast<LPCWSTR>(&InterceptCreate),&resident_module)) return GetLastError();
    ui_tid=GetCurrentThreadId();matches=0;timer_expired=false;restore_attempts=0;cleanup_unresolved=false;
    shows_before=WxBgFileOpenProxyCancelledShowCount();
    accepted_before=WxBgFileOpenProxyAcceptedShowCount();selection_mode=data->command==5?1:0;
    lease_deadline=GetTickCount64()+4000;
    InterlockedIncrement64(&positive_grant.generation);
    InterlockedExchange(&positive_grant.allowed,selection_mode==1?1:0);
    timer=SetTimer(nullptr,0,4000,Expire);
    if (!timer) {DWORD failure=GetLastError();InterlockedExchange(&positive_grant.allowed,0);fixture_grant.Close();return failure?failure:ERROR_GEN_FAILURE;}
    InterlockedExchange(&active,1);
    last_result=lease.Install(image,slot,reinterpret_cast<void *>(original),reinterpret_cast<void *>(&InterceptCreate));
    if (last_result.code!=wxbg::IatLeaseCode::ok) { Restore();return ERROR_WRITE_FAULT; }
    LPARAM point=MAKELPARAM(data->x,data->y);
    if (!PostMessageW(hwnd,WM_LBUTTONDOWN,MK_LBUTTON,point)) { err=GetLastError();Restore();return err; }
    if (!PostMessageW(hwnd,WM_LBUTTONUP,0,point)) {
        err=GetLastError();PostMessageW(hwnd,WM_LBUTTONUP,0,point);Restore();return err;
    }
    return ERROR_SUCCESS;
}
extern "C" __declspec(dllexport) LRESULT CALLBACK WxBgAttachmentHook(int code,WPARAM wp,LPARAM lp) {
    if (code==HC_ACTION && lp) {
        const auto *message=reinterpret_cast<const CWPSTRUCT *>(lp);
        UINT command=RegisterWindowMessageW(MESSAGE);
        DWORD pid=0,tid=GetWindowThreadProcessId(message->hwnd,&pid);
        if (command && message->message==command && message->wParam && message->lParam==MAGIC &&
            pid==GetCurrentProcessId() && tid==GetCurrentThreadId()) {
            wchar_t name[96];swprintf(name,96,L"Local\\WxBgAttachment4.%lu.%016llx",(unsigned long)pid,(unsigned long long)message->wParam);
            HANDLE mapping=OpenFileMappingW(FILE_MAP_READ|FILE_MAP_WRITE,FALSE,name);
            if (mapping) {
                auto *data=static_cast<Data *>(MapViewOfFile(mapping,FILE_MAP_READ|FILE_MAP_WRITE,0,0,sizeof(Data)));
                if(data) {
                    ULONGLONG now=GetTickCount64();
                    if(data->magic==MAGIC && data->version==4 && data->size==sizeof(Data) &&
                        data->nonce==message->wParam && data->pid==pid && data->tid==tid &&
                        data->request_deadline>=now && data->request_deadline-now<=10000 &&
                        data->command>=1 && data->command<=5 && InterlockedCompareExchange(&data->state,1,0)==0) {
                        if(InterlockedCompareExchange(&busy,1,0)!=0) data->error=ERROR_BUSY;
                        else {
                            if (data->command==1) data->error=(lease.installed() || lease.protection_pending())?ERROR_BUSY:Resolve(data->kind);
                            else if(data->command==2 || data->command==5) data->error=Arm(data,message->hwnd);
                            else if(data->command==4) Restore();
                            Snapshot(data);InterlockedExchange(&busy,0);
                        }
                        InterlockedExchange(&data->state,2);
                    }
                    UnmapViewOfFile(data);
                }
                CloseHandle(mapping);
            }
        }
    }
    return CallNextHookEx(nullptr,code,wp,lp);
}
BOOL WINAPI DllMain(HINSTANCE instance,DWORD reason,LPVOID) {
    if(reason==DLL_PROCESS_ATTACH) DisableThreadLibraryCalls(instance);
    return TRUE;
}

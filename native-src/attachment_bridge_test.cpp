// White-box tests run only in this owned process; no Weixin access.
#include "attachment_bridge.cpp"
#include <assert.h>
static int checks=0;
#define CHECK(v) do { if(!(v)){fprintf(stderr,"FAIL line %d: %s\n",__LINE__,#v);return 1;}++checks; } while(0)
int main() {
    CHECK(SUCCEEDED(CoInitializeEx(nullptr,COINIT_APARTMENTTHREADED)));
    CHECK(LoadLibraryW(L"attachment_fixture.dll")!=nullptr);
    CHECK(Resolve(0)==0);
    WNDCLASSW wc={};wc.lpfnWndProc=DefWindowProcW;wc.hInstance=GetModuleHandleW(nullptr);wc.lpszClassName=L"Qt51514QWindowIcon";
    CHECK(RegisterClassW(&wc));
    HWND hwnd=CreateWindowW(wc.lpszClassName,L"hidden bridge state test",WS_POPUP,0,0,3235,1995,nullptr,nullptr,wc.hInstance,nullptr);
    CHECK(hwnd && !IsWindowVisible(hwnd));
    WNDCLASSW render_class={};render_class.lpfnWndProc=DefWindowProcW;
    render_class.hInstance=wc.hInstance;render_class.lpszClassName=L"MMUIRenderSubWindowHW";
    CHECK(RegisterClassW(&render_class));
    HWND render=CreateWindowW(render_class.lpszClassName,L"",WS_CHILD,0,0,3235,1995,
                              hwnd,nullptr,wc.hInstance,nullptr);
    CHECK(render && ValidProductionAttachmentLayout(hwnd,923,1910,3235,1995));
    CHECK(!ValidProductionAttachmentLayout(hwnd,922,1955,3240,2040));
    CHECK(!ValidProductionAttachmentLayout(hwnd,923,1400,3235,1995));
    CHECK(SetWindowPos(hwnd,nullptr,0,0,3240,2040,SWP_NOZORDER));
    CHECK(SetWindowPos(render,nullptr,0,0,3240,2040,SWP_NOZORDER));
    CHECK(ValidProductionAttachmentLayout(hwnd,922,1955,3240,2040));
    CHECK(!ValidProductionAttachmentLayout(hwnd,923,1910,3235,1995));
    HWND duplicate=CreateWindowW(render_class.lpszClassName,L"",WS_CHILD,0,0,3240,2040,
                                 hwnd,nullptr,wc.hInstance,nullptr);
    CHECK(duplicate && !ValidProductionAttachmentLayout(hwnd,922,1955,3240,2040));
    CHECK(DestroyWindow(duplicate));
    Data data={};data.kind=0;data.x=12;data.y=12;
    CHECK(Arm(&data,hwnd)==0);CHECK(lease.installed() && active==1);
    UINT_PTR old_timer=timer;
    Expire(nullptr,0,timer+1,0);CHECK(lease.installed() && !timer_expired);
    Expire(nullptr,0,timer,0);CHECK(lease.installed() && !timer_expired);
    lease_deadline=0;Expire(nullptr,0,timer,0);
    CHECK(!lease.installed() && !timer && !active && *slot==reinterpret_cast<void *>(original));
    CHECK(Arm(&data,hwnd)==0);
    Expire(nullptr,0,old_timer,0);CHECK(lease.installed() && !timer_expired);
    // Conflict: another owner replaced our pointer. Never overwrite its choice.
    void *external=reinterpret_cast<void *>(&Resolve);
    InterlockedExchangePointer(slot,external);
    Restore();CHECK(cleanup_unresolved && restore_attempts==1 && timer && active==1 && *slot==external);
    Restore();CHECK(cleanup_unresolved && restore_attempts==2 && timer && active==1 && *slot==external);
    Restore();CHECK(cleanup_unresolved && restore_attempts==3 && !timer && active==0 && *slot==external);
    CHECK(Arm(&data,hwnd)==ERROR_BUSY);
    // Remaining wrapper is transparent once cleanup is exhausted.
    IFileOpenDialog *native=nullptr;
    CHECK(SUCCEEDED(InterceptCreate(CLSID_FileOpenDialog,nullptr,CLSCTX_INPROC_SERVER,IID_IFileOpenDialog,reinterpret_cast<void **>(&native))));
    CHECK(native!=nullptr && WxBgFileOpenProxyLiveCount()==0);native->Release();
    // Our owned test now restores only the exact pointer it installed above.
    CHECK(InterlockedCompareExchangePointer(slot,reinterpret_cast<void *>(&InterceptCreate),external)==external);
    Restore();CHECK(!lease.installed() && !cleanup_unresolved && *slot==reinterpret_cast<void *>(original));
    // A positive proxy created before Restore must reject its later Show even
    // though its own creation lease has not elapsed and its vtable is resident.
    wchar_t directory[MAX_PATH]={},file[MAX_PATH]={};
    CHECK(GetTempPathW(MAX_PATH,directory)>0 && GetTempFileNameW(directory,L"wbg",0,file));
    HANDLE output=CreateFileW(file,GENERIC_WRITE,0,nullptr,TRUNCATE_EXISTING,FILE_ATTRIBUTE_NORMAL,nullptr);
    CHECK(output!=INVALID_HANDLE_VALUE);DWORD written=0;
    CHECK(WriteFile(output,"abc",3,&written,nullptr) && written==3);CloseHandle(output);
    CHECK(SUCCEEDED(original(CLSID_FileOpenDialog,nullptr,CLSCTX_INPROC_SERVER,IID_IFileOpenDialog,reinterpret_cast<void **>(&native))));
    InterlockedIncrement64(&positive_grant.generation);InterlockedExchange(&positive_grant.allowed,1);
    WxBgFileProxyGrant grant={static_cast<ULONGLONG>(positive_grant.generation),GetTickCount64()+4000,AuthorizePositive,&positive_grant};
    IFileOpenDialog *delayed=nullptr;
    CHECK(SUCCEEDED(CreateWxBgFileOpenProxyWithGrant(native,file,&grant,&delayed)));native->Release();
    Restore();CHECK(!positive_grant.allowed);
    CHECK(delayed->Show(hwnd)==HRESULT_FROM_WIN32(ERROR_CANCELLED));
    IShellItemArray *items=nullptr;CHECK(FAILED(delayed->GetResults(&items)) && !items);
    delayed->Release();CHECK(WxBgFileOpenProxyLiveCount()==0);
    CHECK(DeleteFileW(file));
    CHECK(DestroyWindow(hwnd));CoUninitialize();
    printf("PASS %d bridge lifecycle checks\n",checks);return 0;
}

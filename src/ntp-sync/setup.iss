; NTP Time Sync (China Standard Time) - Inno Setup script
; Build: ISCC.exe setup.iss
; Target: Windows 7 SP1 and later, 32-bit (runs on x64 via WOW64)

#define AppName "NTP Time Sync"
#define AppVersion "1.3.0"
#define AppPublisher "zc team"

[Setup]
AppId={{A7F3C1D2-5E6B-4A9C-8D21-4C7B9E3F1A56}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={commonpf}\NtpSync
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=_build\out
OutputBaseFilename=ntp_sync_setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
MinVersion=6.1sp1
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\python.exe
CloseApplications=no
RestartApplications=no
AllowNoIcons=yes

[Files]
Source: "dist\ntp_sync_win7_x86\*"; DestDir: "{app}"; \
    Flags: ignoreversion recursesubdirs createallsubdirs; \
    Excludes: "service.log,service.log.*,service.ini,__pycache__\*,*.pyc"

[Icons]
Name: "{group}\Sync clock now"; Filename: "{app}\sync.bat"; WorkingDir: "{app}"; Comment: "Sync the clock immediately (asks for UAC)"
Name: "{group}\Check clock (read-only)"; Filename: "{app}\check.bat"; WorkingDir: "{app}"; Comment: "Report the offset without touching the clock"
Name: "{group}\Service log"; Filename: "{app}\service.log"; Comment: "Log written by the background service"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{commondesktop}\NTP Time Sync"; Filename: "{app}\sync.bat"; WorkingDir: "{app}"

[Run]
Filename: "{app}\python.exe"; Parameters: "ntpsvc.py set-tz"; \
    StatusMsg: "Setting time zone to China Standard Time (UTC+8)..."; \
    Flags: runhidden waituntilterminated
Filename: "{app}\python.exe"; Parameters: "ntpsvc.py install --interval 1800"; \
    StatusMsg: "Registering and starting the Windows service..."; \
    Flags: runhidden waituntilterminated

[UninstallRun]
Filename: "{app}\python.exe"; Parameters: "ntpsvc.py uninstall"; \
    RunOnceId: "StopAndDeleteService"; Flags: runhidden waituntilterminated

[UninstallDelete]
Type: files; Name: "{app}\service.log"
Type: files; Name: "{app}\service.log.*"
Type: files; Name: "{app}\service.ini"
Type: filesandordirs; Name: "{app}\__pycache__"

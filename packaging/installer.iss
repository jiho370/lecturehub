; Lecture Hub installer (Inno Setup 7). Built by tools/build_app.py --installer:
;   ISCC /DAppVersion=1.2.3 /DSourceDir=<dist\LectureHub> /DOutputDir=<out> /DIconFile=<app.ico> installer.iss
;
; Decisions (release review 2026-11-16):
; - per-user install, no administrator rights: {localappdata}\Programs\LectureHub;
; - the app never writes next to its exe, so replacing the folder on update is safe: _internal and bin are
;   removed first (no stale module of an older version stays behind);
; - uninstall removes the program only. The semester folder, the settings (%APPDATA%\LectureHub) and the logs
;   (%LOCALAPPDATA%\LectureHub) stay, and the last page says where they are;
; - the app holds the mutex "LectureHub.AppRunning" while it runs, including hidden (core/single_instance.py): setup and
;   uninstall ask the user to close it instead of replacing files under a running recording.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #error SourceDir (the built LectureHub folder) is required
#endif
#ifndef OutputDir
  #define OutputDir "."
#endif

[Setup]
AppId={{6E0B3A52-7C1D-4E6F-9B7A-2F4C8D1A5E90}
AppName=Lecture Hub
AppVersion={#AppVersion}
AppVerName=Lecture Hub {#AppVersion}
AppPublisher=Lecture Hub contributors
VersionInfoVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\LectureHub
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
OutputDir={#OutputDir}
OutputBaseFilename=LectureHub-{#AppVersion}-win64-setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName=Lecture Hub
UninstallDisplayIcon={app}\LectureHub.exe
AppMutex=LectureHub.AppRunning
CloseApplications=no
#ifdef IconFile
SetupIconFile={#IconFile}
#endif

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[InstallDelete]
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\bin"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Lecture Hub"; Filename: "{app}\LectureHub.exe"; AppUserModelID: "LectureHub.Public"
Name: "{autodesktop}\Lecture Hub"; Filename: "{app}\LectureHub.exe"; AppUserModelID: "LectureHub.Public"; Tasks: desktopicon

[Run]
Filename: "{app}\LectureHub.exe"; Description: "{cm:LaunchProgram,Lecture Hub}"; Flags: nowait postinstall skipifsilent

[CustomMessages]
korean.NoWebView2=이 PC에 화면을 그리는 Windows 부품(Microsoft Edge WebView2 Runtime)이 보이지 않습니다.%n%n설치는 계속되지만, 앱을 처음 열 때 안내에 따라 WebView2를 설치해야 합니다.%nhttps://developer.microsoft.com/microsoft-edge/webview2/
english.NoWebView2=The Microsoft Edge WebView2 Runtime was not found on this PC.%n%nSetup continues, but the app will ask you to install WebView2 the first time it opens.%nhttps://developer.microsoft.com/microsoft-edge/webview2/
korean.DataKept=Lecture Hub 프로그램을 지웠습니다.%n%n아래는 그대로 두었습니다. 필요 없으면 직접 지우세요.%n- 학기 폴더(녹음·전사·정리본이 있는 폴더)%n- 설정: %1%n- 기록과 도우미 작업 폴더: %2
english.DataKept=Lecture Hub was removed.%n%nThese were kept. Delete them yourself if you no longer need them.%n- your semester folder (recordings, transcripts, notes)%n- settings: %1%n- logs and helper workspace: %2

[Code]
function WebView2Version(): String;
var
  Value: String;
begin
  Result := '';
  if RegQueryStringValue(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Value)
     or RegQueryStringValue(HKLM, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Value)
     or RegQueryStringValue(HKCU, 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Value) then
    if Value <> '0.0.0.0' then
      Result := Value;
end;

function InitializeSetup(): Boolean;
begin
  Result := True;
  if (WebView2Version() = '') and not WizardSilent() then
    MsgBox(CustomMessage('NoWebView2'), mbInformation, MB_OK);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  StartupCommand: String;
begin
  { Only remove this install's opt-in Run entry; another zip/install may own the value now. }
  if (CurUninstallStep = usUninstall) and
     RegQueryStringValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'LectureHub', StartupCommand) then
    if (CompareText(StartupCommand, '"' + ExpandConstant('{app}\LectureHub.exe') + '" --background') = 0) or
       (CompareText(StartupCommand, ExpandConstant('{app}\LectureHub.exe') + ' --background') = 0) then
      RegDeleteValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'LectureHub');
  if (CurUninstallStep = usPostUninstall) and not UninstallSilent() then
    MsgBox(FmtMessage(CustomMessage('DataKept'), [ExpandConstant('{userappdata}\LectureHub'), ExpandConstant('{localappdata}\LectureHub')]),
           mbInformation, MB_OK);
end;

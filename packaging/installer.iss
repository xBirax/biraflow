; Inno Setup script for Bira Flow. Built by scripts\build-installer.ps1:
;   ISCC /DAppVersion=1.0.0 /Sbfsign="..." packaging\installer.iss
; Per-user install (no administrator rights), so updates can also run silently.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6E3C9B4A-2F1D-4C7E-9A8B-B1F2A0F10E01}
AppName=Bira Flow
AppVersion={#AppVersion}
AppVerName=Bira Flow {#AppVersion}
AppPublisher=Bira Flow
AppComments=Ditado por voz em qualquer aplicativo
VersionInfoVersion={#AppVersion}
VersionInfoProductName=Bira Flow
DefaultDirName={localappdata}\Programs\Bira Flow
DefaultGroupName=Bira Flow
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=BiraFlow-Setup-{#AppVersion}
SetupIconFile=..\biraflow\assets\app.ico
UninstallDisplayIcon={app}\biraflow.exe
UninstallDisplayName=Bira Flow
WizardStyle=modern
WizardImageFile=wizard-large.bmp
WizardSmallImageFile=wizard-small.bmp
Compression=lzma2/ultra64
SolidCompression=yes
; the running app holds its own files open: close it (updates also quit it themselves)
CloseApplications=force
RestartApplications=no
SignTool=bfsign
SignedUninstaller=yes

[Languages]
Name: "pt"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"

[Tasks]
Name: "autostart"; Description: "Iniciar o Bira Flow junto com o Windows"
Name: "desktopicon"; Description: "Criar um atalho na área de trabalho"; Flags: unchecked

[Files]
Source: "..\dist\BiraFlow\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[InstallDelete]
; a new build may not contain every file of the previous one
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
; same AppUserModelID as the running app: its taskbar button and pins use this shortcut's icon
Name: "{userprograms}\Bira Flow"; Filename: "{app}\biraflow.exe"; Comment: "Ditado por voz em qualquer aplicativo"; AppUserModelID: "BiraFlow.Dictation"
Name: "{userdesktop}\Bira Flow"; Filename: "{app}\biraflow.exe"; Tasks: desktopicon; AppUserModelID: "BiraFlow.Dictation"
; right-click an audio file -> Enviar para -> Bira Flow: opens it in "Transcrever áudio"
Name: "{usersendto}\Bira Flow (transcrever áudio)"; Filename: "{app}\biraflow.exe"

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "BiraFlow"; ValueData: """{app}\biraflow.exe"""; Tasks: autostart; Flags: uninsdeletevalue

[Run]
; refresh Windows' icon cache so an already pinned taskbar button shows the new icon
Filename: "{sys}\ie4uinit.exe"; Parameters: "-show"; Flags: runhidden nowait skipifdoesntexist
; interactive install: offer to open it; update made by the app (/RELAUNCH=1): reopen it
Filename: "{app}\biraflow.exe"; Description: "Abrir o Bira Flow"; Flags: nowait postinstall skipifsilent
Filename: "{app}\biraflow.exe"; Flags: nowait runasoriginaluser; Check: RelaunchAfterUpdate

[UninstallRun]
; only processes started from this installation (whisper-server dies with the app)
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -NonInteractive -Command ""Get-Process biraflow,biraflow-cli -ErrorAction SilentlyContinue | Where-Object {{ $_.Path -like '{code:AppDirForPowerShell}\*' } | Stop-Process -Force"""; Flags: runhidden; RunOnceId: "StopBiraFlow"

[UninstallDelete]
Type: filesandordirs; Name: "{app}\_internal"

[Code]
// {app} inside a single-quoted PowerShell string: double any apostrophe (C:\Users\O'Brien)
function AppDirForPowerShell(Param: String): String;
begin
  Result := ExpandConstant('{app}');
  StringChangeEx(Result, '''', '''''', True);
end;

function RelaunchAfterUpdate(): Boolean;
begin
  Result := WizardSilent() and (ExpandConstant('{param:RELAUNCH|0}') = '1');
end;

[Messages]
pt.FinishedLabel=O Bira Flow foi instalado. Segure Ctrl+Win e fale para ditar em qualquer aplicativo.%n%nSuas configurações, chaves e modelos ficam em %APPDATA%\BiraFlow e %LOCALAPPDATA%\BiraFlow e são mantidos ao desinstalar.

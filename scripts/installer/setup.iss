; Inno Setup 6 script - standard Windows installer for Super Brain v3
; build: ISCC.exe setup.iss   (run build.ps1 first to prepare dist\)
[Setup]
AppId={{8E7B2C10-5B1C-4C6B-9F1A-3D7E9A0C2B11}
AppName=Super Brain v3
AppVersion=3.0.0
AppPublisher=Super Brain
DefaultDirName={autopf}\SuperBrain
DefaultGroupName=Super Brain v3
DisableProgramGroupPage=yes
OutputDir=..\..\release
OutputBaseFilename=SuperBrainV3Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64
PrivilegesRequired=admin
UninstallDisplayIcon={app}\app\v3\web\index.html

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "..\..\dist\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "super-brain-v3.xml"; DestDir: "{app}"; Flags: ignoreversion

[Dirs]
Name: "{commonappdata}\SuperBrain\data"; Permissions: users-modify
Name: "{commonappdata}\SuperBrain\logs"; Permissions: users-modify

[Run]
Filename: "{app}\winsw.exe"; Parameters: "install"; Flags: runhidden; StatusMsg: "Registering memory service..."
Filename: "{app}\winsw.exe"; Parameters: "start"; Flags: runhidden; StatusMsg: "Starting memory service..."
Filename: "cmd.exe"; Parameters: "/c start http://127.0.0.1:8768/"; Flags: runhidden shellexec; Check: not WizardSilent

[UninstallRun]
Filename: "{app}\winsw.exe"; Parameters: "stop"; Flags: runhidden; RunOnceId: "StopService"
Filename: "{app}\winsw.exe"; Parameters: "uninstall"; Flags: runhidden; RunOnceId: "RemoveService"

[Icons]
Name: "{group}\Super Brain Nebula"; Filename: "http://127.0.0.1:8768/"
Name: "{autodesktop}\Super Brain Nebula"; Filename: "http://127.0.0.1:8768/"; Tasks: desktopicon

[Code]
// Data directory is intentionally NOT removed on uninstall (user memory survives).
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    MsgBox('Your memory data in ' + ExpandConstant('{commonappdata}\SuperBrain\data') + ' was kept.', mbInformation, MB_OK);
end;

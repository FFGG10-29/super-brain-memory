; Inno Setup 6 script - Super Brain: Flutter frontend + frozen Python backend
; build: ISCC.exe setup.iss   (run build.ps1 first to prepare dist\)
[Setup]
AppId={{8E7B2C10-5B1C-4C6B-9F1A-3D7E9A0C2B11}
AppName=Super Brain
AppVersion=3.1.0
AppPublisher=Super Brain
DefaultDirName={autopf}\SuperBrain
DefaultGroupName=Super Brain
DisableProgramGroupPage=yes
OutputDir=..\..\release
OutputBaseFilename=SuperBrainSetup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64
PrivilegesRequired=admin
UninstallDisplayIcon={app}\super_brain.exe

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
; dist\ contains super_brain.exe + data\ (Flutter) and backend\ (frozen headless server).
; Exclude the intermediate PyInstaller output directory and optional service files.
Source: "..\..\dist\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "super-brain-backend\*,winsw.exe,super-brain-v3.xml"

[Dirs]
Name: "{commonappdata}\SuperBrain\data"; Permissions: users-modify
Name: "{commonappdata}\SuperBrain\logs"; Permissions: users-modify

[Run]
; Flutter starts first and launches backend\headless-server.exe itself (process separation).
Filename: "{app}\super_brain.exe"; Description: "Launch Super Brain"; Flags: nowait postinstall skipifsilent shellexec

[Icons]
Name: "{group}\Super Brain"; Filename: "{app}\super_brain.exe"
Name: "{autodesktop}\Super Brain"; Filename: "{app}\super_brain.exe"; Tasks: desktopicon

[Code]
// Memory data is intentionally NOT removed on uninstall (user memory survives).
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    MsgBox('Your memory data in ' + ExpandConstant('{userappdata}\super-brain\data') + ' was kept.', mbInformation, MB_OK);
end;

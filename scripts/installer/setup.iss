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
; 安装时必须允许用户选择落地位置（默认即为 no，此处显式声明以防被覆盖）
DisableDirPage=no
AlwaysShowDirOnReadyPage=yes
OutputDir=..\..\release
OutputBaseFilename=SuperBrainSetup
; 单文件本地分发：降低 lzma2 级别并关闭固实压缩，压缩内存/CPU 压力大幅下降。
; 原因：lzma2/max + solid 压缩 4GB 时会与常驻后端抢资源，后端曾崩在 ucrtbase.dll。
; 包体略大几十 MB，换来压缩时间减半且 MCP 服务可保持在线。
Compression=lzma2/normal
SolidCompression=no
WizardStyle=modern
; 沿用旧 Qt/Python 前端的品牌图标（多尺寸 16~256）
SetupIconFile=..\..\ui\desktop\assets\app.ico
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
; 允许普通用户选择"仅为我安装"，此时可写入非 Program Files 的自定义目录
PrivilegesRequiredOverridesAllowed=dialog
UninstallDisplayIcon={app}\super_brain.exe

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
; dist\ contains super_brain.exe + data\ (Flutter) and backend\ (frozen headless server).
; 必须排除两个 PyInstaller 中间产物目录：super-brain\ 是旧 Qt 桌面冻结包（13GB，含 PySide6），
; super-brain-backend\ 是后端构建产物（已复制到 dist\backend\）。漏排会把 22GB+ 且带 Qt 的
; 内容打进安装包，违反 ADR-022「Qt 不进发行包」。
Source: "..\..\dist\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "super-brain\*,super-brain-backend\*,winsw.exe,super-brain-v3.xml,*.log,*.pyc"

; 不预建数据目录：core/runtime_paths.py::user_dir()/data_dir() 启动时自行 mkdir，
; 且冻结态数据落 %APPDATA%\super-brain\data（每用户区）。安装以 admin 运行时预建
; 每用户目录会指向错误的用户并触发 Inno Setup 的 UsedUserAreasWarning。

[Run]
; Flutter starts first and launches backend\headless-server.exe itself (process separation).
; runasoriginaluser：安装若提权，仍以当前登录用户启动，避免数据写到管理员的 APPDATA。
Filename: "{app}\super_brain.exe"; Description: "Launch Super Brain"; Flags: nowait postinstall skipifsilent shellexec runasoriginaluser

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

; Instalador de Clipper (Inno Setup 6). Lo compila el workflow de GitHub.

[Setup]
AppId={{DF690311-EED1-4514-B47B-1B91401091BF}
AppName=Clipper
AppVersion=2.0
AppPublisher=Y4ll
DefaultDirName={autopf}\Clipper
DefaultGroupName=Clipper
OutputDir=output
OutputBaseFilename=ClipperSetup
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\Clipper.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"

[Tasks]
Name: "desktopicon"; Description: "Crear un acceso directo en el escritorio"; GroupDescription: "Accesos directos:"

[Files]
Source: "pack\Clipper.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "pack\ffmpeg.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "pack\whisper\*"; DestDir: "{app}\whisper"; Flags: ignoreversion recursesubdirs skipifsourcedoesntexist

[Icons]
Name: "{group}\Clipper"; Filename: "{app}\Clipper.exe"
Name: "{autodesktop}\Clipper"; Filename: "{app}\Clipper.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Clipper.exe"; Description: "Abrir Clipper"; Flags: nowait postinstall skipifsilent

; Inno Setup 6 script - builds dist\kdenlive-blur-gui-setup-<version>.exe
; Build:  open this file in Inno Setup (https://jrsoftware.org/isinfo.php) and press Compile,
;         or run   ISCC.exe packaging\windows\kdenlive-blur-gui.iss
; Needs a LICENSE file in the repository root.
#define MyAppName "Kdenlive Auto-Blur Tool"
#define MyAppVersion "1.0.1"
#define MyAppPublisher "Kdenlive Auto-Blur Tool contributors"
#define MyAppURL "https://github.com/YOUR-USER/kdenlive-blur-gui"

[Setup]
AppId={{16CD1B69-5451-4A27-BC1D-C49FA0431A4D}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
DefaultDirName={autopf}\KdenliveAutoBlurTool
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; installs for the current user only, no administrator rights needed
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
LicenseFile=..\..\LICENSE
SetupIconFile=..\..\assets\kdenlive-blur-gui.ico
UninstallDisplayIcon={app}\kdenlive-blur-gui.ico
OutputDir=..\..\dist
OutputBaseFilename=kdenlive-blur-gui-setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop icon"; GroupDescription: "Additional icons:"; Flags: unchecked

[Files]
Source: "..\..\kdenlive_blur_gui.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\kdenlive-blur-gui.png"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\assets\kdenlive-blur-gui.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{code:GetPythonw}"; Parameters: """{app}\kdenlive_blur_gui.py"""; WorkingDir: "{app}"; IconFilename: "{app}\kdenlive-blur-gui.ico"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{code:GetPythonw}"; Parameters: """{app}\kdenlive_blur_gui.py"""; WorkingDir: "{app}"; IconFilename: "{app}\kdenlive-blur-gui.ico"; Tasks: desktopicon

[Run]
Filename: "{code:GetPythonw}"; Parameters: """{app}\kdenlive_blur_gui.py"""; WorkingDir: "{app}"; Description: "Start {#MyAppName} now"; Flags: nowait postinstall skipifsilent

[Code]
{ Looks Python up the way PEP 514 describes (registry), newest version first. }
function PythonwFor(RootKey: Integer; Minor: Integer): String;
var
  Key, S: String;
begin
  Result := '';
  Key := 'Software\Python\PythonCore\3.' + IntToStr(Minor) + '\InstallPath';
  if RegQueryStringValue(RootKey, Key, 'WindowedExecutablePath', S) and FileExists(S) then
    Result := S
  else if RegQueryStringValue(RootKey, Key, 'ExecutablePath', S) then
  begin
    S := ExtractFilePath(S) + 'pythonw.exe';
    if FileExists(S) then
      Result := S;
  end;
end;

function FindPythonw(): String;
var
  Minor: Integer;
begin
  Result := '';
  for Minor := 14 downto 10 do
  begin
    Result := PythonwFor(HKCU, Minor);
    if Result <> '' then Exit;
    Result := PythonwFor(HKLM, Minor);
    if Result <> '' then Exit;
  end;
end;

{ Used by the shortcuts; falls back to the py launcher if Python was not found. }
function GetPythonw(Param: String): String;
begin
  Result := FindPythonw();
  if Result = '' then
    Result := 'pyw.exe';
end;

function InitializeSetup(): Boolean;
var
  ResultCode: Integer;
begin
  Result := True;
  if FindPythonw() = '' then
  begin
    if MsgBox('The tool needs Python 3.10 or newer, which was not found on this computer.' + #13#10 + #13#10 +
              'Install Python 3.12 now with winget?' + #13#10 +
              '(Choose "No" to install Python yourself from python.org.)',
              mbConfirmation, MB_YESNO) = IDYES then
    begin
      Exec(ExpandConstant('{cmd}'),
           '/C winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements',
           '', SW_SHOW, ewWaitUntilTerminated, ResultCode);
      if FindPythonw() = '' then
        MsgBox('Python could not be detected after the installation. You can continue; ' +
               'the tool will start as soon as Python is available.', mbInformation, MB_OK);
    end
    else
      MsgBox('Please install Python 3.10 or newer from https://www.python.org/downloads/ ' +
             '(keep the "tcl/tk and IDLE" option ticked). The tool cannot start before that.',
             mbInformation, MB_OK);
  end;
end;

{ The components downloaded on first start live outside the install folder. }
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    if MsgBox('Also remove the Python components the tool downloaded (about 1 GB)?',
              mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
      DelTree(ExpandConstant('{localappdata}\KdenliveBlurApp'), True, True, True);
  end;
end;

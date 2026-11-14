Unicode true

!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "FileFunc.nsh"
!include "x64.nsh"

!ifndef MyAppVersion
  !define MyAppVersion "1.0.3"
!endif

!define REPO_ROOT "..\.."
!define APP_NAME "Kdenlive Auto-Blur Tool"
!define APP_KEY "KdenliveAutoBlurTool"
!define APP_ICON "${REPO_ROOT}\assets\kdenlive-blur-gui.ico"

Name "${APP_NAME} ${MyAppVersion}"
OutFile "${REPO_ROOT}\dist\kdenlive-blur-gui-setup-${MyAppVersion}.exe"
InstallDir "$LOCALAPPDATA\Programs\KdenliveAutoBlurTool"
RequestExecutionLevel user
SetCompressor /SOLID lzma

VIProductVersion "${MyAppVersion}.0"
VIAddVersionKey /LANG=1033 "ProductName" "${APP_NAME}"
VIAddVersionKey /LANG=1033 "ProductVersion" "${MyAppVersion}"
VIAddVersionKey /LANG=1033 "FileVersion" "${MyAppVersion}"
VIAddVersionKey /LANG=1033 "FileDescription" "${APP_NAME} installer"
VIAddVersionKey /LANG=1033 "LegalCopyright" "Kdenlive Auto-Blur Tool contributors"

!define MUI_ICON "${APP_ICON}"
!define MUI_UNICON "${APP_ICON}"
!define MUI_ABORTWARNING
!define MUI_FINISHPAGE_RUN
!define MUI_FINISHPAGE_RUN_TEXT "Start ${APP_NAME} now"
!define MUI_FINISHPAGE_RUN_FUNCTION "LaunchApp"

!insertmacro MUI_PAGE_LICENSE "${REPO_ROOT}\LICENSE"
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

Var Pythonw

Function .onInit
  SetShellVarContext current
  Call FindPythonw
  ${If} $Pythonw == ""
    MessageBox MB_YESNO|MB_ICONQUESTION "This tool needs Python 3.10 or newer, which was not found on this computer.$\r$\n$\r$\nInstall Python 3.12 now with winget? Choose No to install Python yourself from python.org." IDYES install_python
    MessageBox MB_OK|MB_ICONINFORMATION "Please install Python 3.10 or newer from https://www.python.org/downloads/ and keep the Tcl/Tk option enabled. The tool cannot start before that."
    Goto python_prompt_done

  install_python:
    ExecWait '"$SYSDIR\cmd.exe" /C winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements' $0
    Call FindPythonw
    ${If} $Pythonw == ""
      MessageBox MB_OK|MB_ICONINFORMATION "Python could not be detected after installation. You can continue; the tool will start as soon as Python is available."
    ${EndIf}
  python_prompt_done:
  ${EndIf}
FunctionEnd

!macro CheckPythonRoot ROOT VIEW
  ${If} $Pythonw == ""
    StrCpy $1 14

    python_lookup_loop_${ROOT}_${VIEW}:
      StrCpy $2 "Software\Python\PythonCore\3.$1\InstallPath"
      ReadRegStr $Pythonw ${ROOT} "$2" "WindowedExecutablePath"
      ${If} $Pythonw != ""
        IfFileExists "$Pythonw" python_lookup_done_${ROOT}_${VIEW}
        StrCpy $Pythonw ""
      ${EndIf}

      ReadRegStr $0 ${ROOT} "$2" "ExecutablePath"
      ${If} $0 != ""
        ${GetParent} "$0" $Pythonw
        StrCpy $Pythonw "$Pythonw\pythonw.exe"
        IfFileExists "$Pythonw" python_lookup_done_${ROOT}_${VIEW}
        StrCpy $Pythonw ""
      ${EndIf}

      IntOp $1 $1 - 1
      ${If} $1 >= 10
        Goto python_lookup_loop_${ROOT}_${VIEW}
      ${EndIf}

    python_lookup_done_${ROOT}_${VIEW}:
  ${EndIf}
!macroend

Function FindPythonw
  StrCpy $Pythonw ""
  ${If} ${RunningX64}
    SetRegView 64
    !insertmacro CheckPythonRoot HKCU 64
    !insertmacro CheckPythonRoot HKLM 64
  ${EndIf}
  SetRegView 32
  !insertmacro CheckPythonRoot HKCU 32
  !insertmacro CheckPythonRoot HKLM 32
FunctionEnd

Function LaunchApp
  Call FindPythonw
  ${If} $Pythonw == ""
    StrCpy $Pythonw "pyw.exe"
  ${EndIf}
  ExecShell "open" "$Pythonw" '"$INSTDIR\kdenlive_blur_gui.py"'
FunctionEnd

Section "${APP_NAME}" SecMain
  SectionIn RO
  SetOutPath "$INSTDIR"
  File "${REPO_ROOT}\kdenlive_blur_gui.py"
  File "${REPO_ROOT}\kdenlive-blur-gui.png"
  File "${APP_ICON}"
  File "${REPO_ROOT}\README.md"
  File "${REPO_ROOT}\LICENSE"

  Call FindPythonw
  ${If} $Pythonw == ""
    StrCpy $Pythonw "pyw.exe"
  ${EndIf}

  CreateDirectory "$SMPROGRAMS\${APP_NAME}"
  CreateShortCut "$SMPROGRAMS\${APP_NAME}\${APP_NAME}.lnk" "$Pythonw" '"$INSTDIR\kdenlive_blur_gui.py"' "$INSTDIR\kdenlive-blur-gui.ico"

  WriteUninstaller "$INSTDIR\uninstall.exe"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_KEY}" "DisplayName" "${APP_NAME}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_KEY}" "DisplayVersion" "${MyAppVersion}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_KEY}" "Publisher" "Kdenlive Auto-Blur Tool contributors"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_KEY}" "DisplayIcon" "$INSTDIR\kdenlive-blur-gui.ico"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_KEY}" "UninstallString" "$\"$INSTDIR\uninstall.exe$\""
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_KEY}" "NoRepair" 1
SectionEnd

Section /o "Create a desktop icon" SecDesktop
  CreateShortCut "$DESKTOP\${APP_NAME}.lnk" "$Pythonw" '"$INSTDIR\kdenlive_blur_gui.py"' "$INSTDIR\kdenlive-blur-gui.ico"
SectionEnd

Section "Uninstall"
  SetShellVarContext current
  Delete "$DESKTOP\${APP_NAME}.lnk"
  Delete "$SMPROGRAMS\${APP_NAME}\${APP_NAME}.lnk"
  RMDir "$SMPROGRAMS\${APP_NAME}"
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_KEY}"

  MessageBox MB_YESNO|MB_ICONQUESTION|MB_DEFBUTTON2 "Also remove the Python components the tool downloaded (about 1 GB)?" IDNO keep_components
  RMDir /r "$LOCALAPPDATA\KdenliveBlurApp"
  keep_components:
  RMDir /r "$INSTDIR"
SectionEnd
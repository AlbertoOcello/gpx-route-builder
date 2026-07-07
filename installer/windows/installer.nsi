; GPX Route Builder — Windows Installer (NSIS)
; Compilare con: makensis installer.nsi

Unicode True
!define APP_NAME    "GPX Route Builder"
!define APP_VERSION "1.0"
!define PUBLISHER   "Alberto Ocello"
!define EXE_NAME    "GPX-Route-Builder-Setup.exe"

Name            "${APP_NAME} ${APP_VERSION}"
OutFile         "${EXE_NAME}"
InstallDir      "$APPDATA\gpx-route-builder"
RequestExecutionLevel user
SetCompressor   /SOLID lzma
BrandingText    "${APP_NAME} ${APP_VERSION} — ${PUBLISHER}"

; ── MUI2 ──────────────────────────────────────────────────────────────────────
!include "MUI2.nsh"

!define MUI_ICON    "icon.ico"
!define MUI_UNICON  "icon.ico"

!define MUI_WELCOMEPAGE_TITLE   "${APP_NAME}"
!define MUI_WELCOMEPAGE_TEXT    "Questo wizard installerà ${APP_NAME} sul tuo computer.$\r$\n$\r$\nL'applicazione richiede Docker Desktop (gratuito). Se non è installato, potrai scaricarlo quando apri l'app per la prima volta.$\r$\n$\r$\nClicca Avanti per continuare."

!define MUI_FINISHPAGE_RUN      "$WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe"
!define MUI_FINISHPAGE_RUN_PARAMETERS '-ExecutionPolicy Bypass -WindowStyle Normal -File $\"$INSTDIR\launcher.ps1$\"'
!define MUI_FINISHPAGE_RUN_TEXT "Avvia ${APP_NAME} ora"
!define MUI_FINISHPAGE_TEXT     "${APP_NAME} è installato.$\r$\n$\r$\n▸ Icona sul Desktop e nel menu Start$\r$\n▸ Al primo avvio inserisci la tua chiave API AI$\r$\n▸ I dati sono salvati in:%APPDATA%\gpx-route-builder\"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "Italian"

; ── Install ───────────────────────────────────────────────────────────────────
Section "Installa ${APP_NAME}" SecMain
    SetOutPath "$INSTDIR"
    File "launcher.ps1"
    File "icon.ico"
    CreateDirectory "$INSTDIR\routes"
    CreateDirectory "$INSTDIR\data"

    ; Shortcut Desktop
    CreateShortCut "$DESKTOP\${APP_NAME}.lnk" \
        "$WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe" \
        '-ExecutionPolicy Bypass -WindowStyle Normal -File $\"$INSTDIR\launcher.ps1$\"' \
        "$INSTDIR\icon.ico" 0

    ; Shortcut Start Menu
    CreateDirectory "$SMPROGRAMS\${APP_NAME}"
    CreateShortCut "$SMPROGRAMS\${APP_NAME}\${APP_NAME}.lnk" \
        "$WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe" \
        '-ExecutionPolicy Bypass -WindowStyle Normal -File $\"$INSTDIR\launcher.ps1$\"' \
        "$INSTDIR\icon.ico" 0

    ; Uninstaller e registro Add/Remove Programs
    WriteUninstaller "$INSTDIR\uninstall.exe"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" \
        "DisplayName"      "${APP_NAME}"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" \
        "UninstallString"  '"$INSTDIR\uninstall.exe"'
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" \
        "DisplayVersion"   "${APP_VERSION}"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" \
        "Publisher"        "${PUBLISHER}"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" \
        "DisplayIcon"      "$INSTDIR\icon.ico"
    WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" \
        "NoModify" 1
    WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" \
        "NoRepair" 1
SectionEnd

; ── Uninstall ─────────────────────────────────────────────────────────────────
Section "Uninstall"
    Delete "$DESKTOP\${APP_NAME}.lnk"
    RMDir /r "$SMPROGRAMS\${APP_NAME}"
    Delete "$INSTDIR\launcher.ps1"
    Delete "$INSTDIR\icon.ico"
    Delete "$INSTDIR\uninstall.exe"
    RMDir  "$INSTDIR"
    DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}"
SectionEnd

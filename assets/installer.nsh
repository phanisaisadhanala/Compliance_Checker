; assets/installer.nsh
; Custom NSIS script — runs during NSIS installer build.
; Add any extra installer steps here.

!macro customInstall
  ; Write a registry key so Windows knows PhasorGrid is installed
  WriteRegStr HKCU "Software\PhasorGrid" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "Software\PhasorGrid" "Version"    "${VERSION}"
!macroend

!macro customUnInstall
  ; Clean up registry on uninstall
  DeleteRegKey HKCU "Software\PhasorGrid"
!macroend

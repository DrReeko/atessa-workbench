; Inno Setup Script for Atessa Toolbelt v0.3.0b1
; Per-user installation under {localappdata}\Programs\Atessa without admin rights

[Setup]
AppId={{D37E7492-91B1-4A55-9B67-8C54148D1052}
AppName=Atessa
AppVersion=0.3.0b1
AppPublisher=Atessa Team
DefaultDirName={localappdata}\Programs\Atessa
DefaultGroupName=Atessa
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputBaseFilename=AtessaSetup-0.3.0b1
Compression=lzma2/max
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
ChangesEnvironment=yes

[Comments]
; All 18 expected executable aliases and native entry modules:
; 1. atessa -> atessa_tui.app:main
; 2. atessa-activity -> atessa_tui.cli:activity_entry
; 3. atessa-arena -> atessa_tui.cli:arena_entry
; 4. atessa-bench -> atessa_tui.cli:bench_entry
; 5. atessa-chat -> atessa_tui.cli:chat_entry
; 6. atessa-council -> atessa_tui.cli:council_entry
; 7. atessa-explain -> atessa_tui.cli:explain_entry
; 8. atessa-ghsearch -> atessa_tui.cli:ghsearch_entry
; 9. atessa-git -> atessa_tui.cli:git_entry
; 10. atessa-image -> atessa_tui.cli:image_entry
; 11. atessa-models -> atessa_tui.cli:models_entry
; 12. atessa-read -> atessa_tui.cli:read_entry
; 13. atessa-search -> atessa_tui.cli:search_entry
; 14. atessa-shell -> atessa_tui.cli:shell_entry
; 15. atessa-shot -> atessa_tui.cli:shot_entry
; 16. atessa-transcribe -> atessa_tui.cli:transcribe_entry
; 17. atessa-view -> atessa_tui.cli:view_entry
; 18. websearch -> atessa_tui.cli:websearch_entry
; Package data handling includes atessa_tui/app.tcss within the bundle.

[Files]
; Copy PyInstaller bundle from dist\atessa (includes executable binaries and atessa_tui/app.tcss)
Source: "dist\atessa\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Atessa"; Filename: "{app}\atessa.exe"; Comment: "Atessa TUI workbench (atessa_tui.app:main)"
Name: "{group}\atessa-activity"; Filename: "{app}\atessa-activity.exe"; Comment: "Atessa Activity CLI (atessa_tui.cli:activity_entry)"
Name: "{group}\atessa-arena"; Filename: "{app}\atessa-arena.exe"; Comment: "Atessa Arena CLI (atessa_tui.cli:arena_entry)"
Name: "{group}\atessa-bench"; Filename: "{app}\atessa-bench.exe"; Comment: "Atessa Bench CLI (atessa_tui.cli:bench_entry)"
Name: "{group}\atessa-chat"; Filename: "{app}\atessa-chat.exe"; Comment: "Atessa Chat CLI (atessa_tui.cli:chat_entry)"
Name: "{group}\atessa-council"; Filename: "{app}\atessa-council.exe"; Comment: "Atessa Council CLI (atessa_tui.cli:council_entry)"
Name: "{group}\atessa-explain"; Filename: "{app}\atessa-explain.exe"; Comment: "Atessa Explain CLI (atessa_tui.cli:explain_entry)"
Name: "{group}\atessa-ghsearch"; Filename: "{app}\atessa-ghsearch.exe"; Comment: "Atessa GHSearch CLI (atessa_tui.cli:ghsearch_entry)"
Name: "{group}\atessa-git"; Filename: "{app}\atessa-git.exe"; Comment: "Atessa Git CLI (atessa_tui.cli:git_entry)"
Name: "{group}\atessa-image"; Filename: "{app}\atessa-image.exe"; Comment: "Atessa Image CLI (atessa_tui.cli:image_entry)"
Name: "{group}\atessa-models"; Filename: "{app}\atessa-models.exe"; Comment: "Atessa Models CLI (atessa_tui.cli:models_entry)"
Name: "{group}\atessa-read"; Filename: "{app}\atessa-read.exe"; Comment: "Atessa Read CLI (atessa_tui.cli:read_entry)"
Name: "{group}\atessa-search"; Filename: "{app}\atessa-search.exe"; Comment: "Atessa Search CLI (atessa_tui.cli:search_entry)"
Name: "{group}\atessa-shell"; Filename: "{app}\atessa-shell.exe"; Comment: "Atessa Shell CLI (atessa_tui.cli:shell_entry)"
Name: "{group}\atessa-shot"; Filename: "{app}\atessa-shot.exe"; Comment: "Atessa Shot CLI (atessa_tui.cli:shot_entry)"
Name: "{group}\atessa-transcribe"; Filename: "{app}\atessa-transcribe.exe"; Comment: "Atessa Transcribe CLI (atessa_tui.cli:transcribe_entry)"
Name: "{group}\atessa-view"; Filename: "{app}\atessa-view.exe"; Comment: "Atessa View CLI (atessa_tui.cli:view_entry)"
Name: "{group}\websearch"; Filename: "{app}\websearch.exe"; Comment: "Websearch CLI (atessa_tui.cli:websearch_entry)"
Name: "{group}\Uninstall Atessa"; Filename: "{uninstallexe}"

[Registry]
; Update user PATH environment variable in HKCU without requiring admin rights
Root: HKCU; Subkey: "Environment"; ValueType: expandsz; ValueName: "Path"; ValueData: "{olddata};{app}"; Check: NeedsAddPath(ExpandConstant('{app}'))

[Code]
function NeedsAddPath(Param: string): boolean;
var
  OrigPath: string;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', OrigPath) then
  begin
    Result := True;
    exit;
  end;
  Result := Pos(';' + Uppercase(Param) + ';', ';' + Uppercase(OrigPath) + ';') = 0;
end;

procedure CurUninstallStepChanged(JustAfter: TUninstallStep);
var
  Path: string;
  AppDir: string;
  P: Integer;
begin
  if JustAfter = usUninstall then
  begin
    AppDir := ExpandConstant('{app}');
    if RegQueryStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', Path) then
    begin
      P := Pos(';' + Uppercase(AppDir) + ';', ';' + Uppercase(Path) + ';');
      if P > 0 then
      begin
        Delete(Path, P, Length(AppDir) + 1);
        RegWriteStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', Path);
      end;
    end;
  end;
end;

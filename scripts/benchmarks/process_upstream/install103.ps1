$d = "$env:LOCALAPPDATA\jarvis-work"
$py = "$d\venv103\Scripts\python.exe"
$log = "<EVIDENCE_OUT_DIR>\smoke103-windows-install.txt"
"" | Out-File -Encoding utf8 $log
foreach ($pkg in @("CoolProp","fluids","ht","thermo","chemicals","biosteam","pyomo","casadi","scikit-sundae","openmdao","fmpy","do-mpc","idaes-pse")) {
  & $py -m pip install --disable-pip-version-check --timeout 120 --retries 8 -q $pkg *> "$d\pip-$pkg.txt"
  "$pkg exit=$LASTEXITCODE" | Out-File -Append -Encoding utf8 $log
}

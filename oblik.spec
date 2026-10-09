# Збірка PyInstaller: один файл OblikVM.exe.
# Запуск: build.bat (або .venv\Scripts\pyinstaller oblik.spec --noconfirm)

a = Analysis(
    ["run.py"],
    pathex=[],
    datas=[
        ("oblik/templates", "oblik/templates"),
        ("oblik/static", "oblik/static"),
        ("oblik/schema", "oblik/schema"),
        ("oblik/docx_templates", "oblik/docx_templates"),
        ("docs/normative", "docs/normative"),   # нормативні документи — у «Довідці»
        ("LICENSE", "."),
    ],
    hiddenimports=[],
    excludes=["tkinter", "pytest"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="OblikVM",
    console=True,  # вікно з повідомленням «не закривайте»
    upx=False,
)

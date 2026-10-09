' Перевірка документів Word: скільки сторінок і PDF «як бачить Word».
' Запуск: cscript //nologo tools\word_check.vbs C:\шлях\файл1.docx [файл2.docx ...]
' Поруч з кожним .docx з'явиться .pdf. Потрібен встановлений Microsoft Word.
Set w = CreateObject("Word.Application")
w.Visible = False
w.DisplayAlerts = 0
For Each f In WScript.Arguments
  Set d = w.Documents.Open(f, False, True)
  WScript.Echo f & ": сторінок " & d.ComputeStatistics(2)
  d.SaveAs2 Left(f, Len(f) - 5) & ".pdf", 17
  d.Close False
Next
w.Quit

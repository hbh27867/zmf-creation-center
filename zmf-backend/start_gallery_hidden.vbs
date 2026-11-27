' ZMF gallery launcher - one-shot hidden start + open page
' (no watchdog, no resident polling: this script starts, waits for the port, opens the page, then exits)
Option Explicit
Dim ws, tries, up, recentUp
Set ws = CreateObject("WScript.Shell")
ws.CurrentDirectory = "C:\tmp_zmf\gallery"

Function PortUp(port)
    PortUp = (ws.Run("powershell -NoProfile -Command ""try{$c=New-Object Net.Sockets.TcpClient;$c.Connect('127.0.0.1'," & port & ");$c.Close();exit 0}catch{exit 1}""", 0, True) = 0)
End Function

up = PortUp(8191)
recentUp = PortUp(8193)
If Not up Then
    ' windowless python (pythonw): no console window is ever created
    ws.Run """C:\Users\hello Teng\3D Objects\ComfyUI-aki-v3-IntelArc_20261031\ComfyUI-aki-v3-IntelArc\python\pythonw.exe"" -I ""C:\tmp_zmf\gallery\server.py""", 0, False
End If
If Not recentUp Then
    ws.Run """C:\Program Files\Python312\pythonw.exe"" ""C:\Users\HELLOT~1\DOCUME~1\COMFYU~1\recent_output_viewer.py"" --no-browser", 0, False
End If
If Not up Or Not recentUp Then
    tries = 0
    Do While tries < 20
        WScript.Sleep 1000
        tries = tries + 1
        up = PortUp(8191)
        recentUp = PortUp(8193)
        If up And recentUp Then Exit Do
    Loop
End If

If up Then
    ws.Run """http://127.0.0.1:8191/""", 1, False
End If
Set ws = Nothing

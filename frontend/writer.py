python3 -c "
import os,re
b='/nfs/yangbb/codes/chat_ds/frontend/src'

# -- App.jsx --
os.makedirs(b,exist_ok=True)
with open(b+'/App.jsx','w') as夫:
  f.write('import{Routes,Route,Navigate}from\"react-router-dom\"\nimport Login from\"./pages/Login\"\nimport Register from\"./pages/Register\"\nimport Chat from\"./pages/Chat\"\nexport default function App(){return(Routes(Route path=\"/login\" element={Login()} Route path=\"/register\" element={Register()} Route path=\"/chat\" element={Chat()} Route path=\"/chat/:convId\" element={Chat()} Route path=\"*\" element={Navigate to=\"/chat\" replace})))')

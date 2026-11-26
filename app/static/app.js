const $ = id => document.getElementById(id);
const fileInput = $('file');
const statusBox = $('status');
let selectedFile = null;
const formatBytes = n => n < 1024*1024 ? (n/1024).toFixed(0)+' KB' : (n/1024/1024).toFixed(2)+' MB';
function showError(message){statusBox.textContent=message;statusBox.classList.remove('hidden');$('progressPanel').classList.add('hidden');$('successPanel').classList.add('hidden')}
function clearMessage(){statusBox.textContent='';statusBox.classList.add('hidden');$('successPanel').classList.add('hidden')}
function setFile(file){
  clearMessage();
  if(!file){selectedFile=null;$('selectedFile').classList.add('hidden');$('filePrompt').textContent='Tarik file ke sini';fileInput.value='';return}
  const ext=(file.name.split('.').pop()||'').toLowerCase();
  if(!['dwg','dxf'].includes(ext)){showError('Format tidak didukung. Pilih file DWG atau DXF.');return}
  if(file.size>32*1024*1024){showError('Ukuran file melebihi batas 32 MB.');return}
  selectedFile=file;$('filePrompt').textContent='File siap diproses';$('fileName').textContent=file.name;$('fileSize').textContent=formatBytes(file.size)+' · '+ext.toUpperCase();$('selectedFile').classList.remove('hidden');$('probeCard').classList.add('hidden');
}
fileInput.addEventListener('change',e=>setFile(e.target.files[0]));
$('removeFile').addEventListener('click',()=>setFile(null));
const dz=$('dropzone');
['dragenter','dragover'].forEach(evt=>dz.addEventListener(evt,e=>{e.preventDefault();dz.classList.add('dragover')}));
['dragleave','drop'].forEach(evt=>dz.addEventListener(evt,e=>{e.preventDefault();dz.classList.remove('dragover')}));
dz.addEventListener('drop',e=>{const f=e.dataTransfer.files[0];if(f){try{const dt=new DataTransfer();dt.items.add(f);fileInput.files=dt.files}catch{}setFile(f)}});
fetch('/crs').then(r=>{if(!r.ok)throw new Error('CRS tidak tersedia');return r.json()}).then(data=>{
  const sel=$('crs');sel.innerHTML='';
  for(const group of data.groups||[]){const og=document.createElement('optgroup');og.label=group.name;for(const opt of group.options||[]){const el=document.createElement('option');el.value=opt.key;el.textContent=opt.label;if(opt.key===data.default)el.selected=true;og.appendChild(el)}sel.appendChild(og)}
}).catch(()=>{});
$('probeBtn').addEventListener('click',async()=>{
  if(!selectedFile){showError('Pilih file DWG atau DXF terlebih dahulu.');return}
  clearMessage();const btn=$('probeBtn');btn.disabled=true;btn.textContent='Memeriksa file…';
  try{const fd=new FormData();fd.append('file',selectedFile);const r=await fetch('/probe',{method:'POST',body:fd});const data=await r.json();if(!r.ok)throw new Error(data.detail||'Gagal memeriksa file.');
    $('probeFormat').textContent=(data.format||'CAD').toUpperCase();const layers=data.layers||[];const features=layers.reduce((sum,l)=>sum+(Number(l.feature_count)||0),0);
    $('probeStats').innerHTML='<div class="stat-chip"><b>'+layers.length+'</b><small>Lapisan</small></div><div class="stat-chip"><b>'+features+'</b><small>Objek terdeteksi</small></div>';
    $('probeOut').textContent=JSON.stringify(layers.map(l=>({nama:l.name,geometri:l.geometry_type,jumlah_fitur:l.feature_count,kolom:l.fields,contoh:l.sample})),null,2);
    $('probeCard').classList.remove('hidden');statusBox.textContent='Pemeriksaan selesai. Isi file berhasil dibaca.';statusBox.classList.remove('hidden');statusBox.style.background='#f0fbff';statusBox.style.color='#126b98';statusBox.style.borderColor='#d7f4ff';
  }catch(err){showError('Tidak dapat membaca isi file: '+err.message)}finally{btn.disabled=false;btn.innerHTML='<span>⌕</span> Periksa isi file'}
});
$('convBtn').addEventListener('click',async()=>{
  if(!selectedFile){showError('Pilih file DWG atau DXF terlebih dahulu.');return}
  const name=$('filename').value.trim().replace(/\.gpkg$/i,'');
  if(!name||!/^[A-Za-z0-9_]+$/.test(name)){showError('Nama file hanya boleh berisi huruf, angka, dan garis bawah (_).');$('filename').focus();return}
  clearMessage();$('successPanel').classList.add('hidden');$('progressPanel').classList.remove('hidden');$('statusTitle').textContent='Sedang mengonversi gambar ukur';$('statusText').textContent='Proses ini dapat memerlukan beberapa saat.';$('progressBar').style.width='18%';$('progressPercent').textContent='RUN';$('spinner').classList.remove('hidden');
  const btn=$('convBtn');btn.disabled=true;btn.querySelector('span').textContent='Memproses…';
  let progress=18;const ticker=setInterval(()=>{progress=Math.min(90,progress+Math.random()*7);$('progressBar').style.width=progress+'%';$('progressPercent').textContent=Math.round(progress)+'%'},900);
  try{const fd=new FormData();fd.append('file',selectedFile);fd.append('crs',$('crs').value);fd.append('filename',name);
    const r=await fetch('/convert',{method:'POST',body:fd});
    if(!r.ok){const data=await r.json().catch(()=>({}));throw new Error(data.detail||'Server mengembalikan status '+r.status)}
    const blob=await r.blob();if(!blob.size)throw new Error('File hasil kosong. Silakan coba kembali.');
    const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download=name+'.gpkg';document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),30000);
    $('progressBar').style.width='100%';$('progressPercent').textContent='100%';$('statusTitle').textContent='Konversi selesai';$('statusText').textContent='File GeoPackage berhasil dibuat dan unduhan dimulai.';$('spinner').classList.add('hidden');
    $('successText').textContent=name+'.gpkg · '+formatBytes(blob.size)+' · siap dibuka di QGIS';$('successPanel').classList.remove('hidden');
  }catch(err){showError('Konversi gagal: '+err.message)}finally{clearInterval(ticker);btn.disabled=false;btn.querySelector('span').textContent='Mulai konversi'}
});
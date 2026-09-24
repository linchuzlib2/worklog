document.addEventListener('DOMContentLoaded', () => {
  const editor = document.querySelector('#editor');
  if (!editor || typeof Quill === 'undefined') return;
  const quill = new Quill(editor, {
    theme: 'snow',
    placeholder: '写下你的内容... ',
    modules: { toolbar: [[{ header: [1, 2, 3, false] }], ['bold', 'italic', 'underline', 'strike'], [{ list: 'ordered' }, { list: 'bullet' }], ['blockquote', 'code-block', 'link'], ['clean']] }
  });
  const form = editor.closest('form');
  form.addEventListener('submit', () => {
    const target = form.querySelector('input[type="hidden"][name="description"], input[type="hidden"][name="content"]');
    target.value = quill.root.innerHTML;
  });
});

document.querySelectorAll('.file-card[draggable="true"]').forEach((file) => {
  file.addEventListener('dragstart', (event) => {
    event.dataTransfer.setData('text/plain', file.dataset.fileId);
    file.classList.add('dragging');
  });
  file.addEventListener('dragend', () => file.classList.remove('dragging'));
});
document.querySelectorAll('.folder-card[data-folder-id]').forEach((folder) => {
  folder.addEventListener('dragover', (event) => { event.preventDefault(); folder.classList.add('drop-target'); });
  folder.addEventListener('dragleave', () => folder.classList.remove('drop-target'));
  folder.addEventListener('drop', async (event) => {
    event.preventDefault();
    folder.classList.remove('drop-target');
    const fileId = event.dataTransfer.getData('text/plain');
    const response = await fetch(`/files/${fileId}/move`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ folder_id: folder.dataset.folderId }) });
    if (response.ok) window.location.reload();
  });
});

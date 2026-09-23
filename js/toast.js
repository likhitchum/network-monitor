const toast=document.getElementById('toast');
export function showToast(title='All systems refreshed',detail='Latest metrics are now available'){toast.querySelector('b').textContent=title;toast.querySelector('small').textContent=detail;toast.classList.add('show');setTimeout(()=>toast.classList.remove('show'),3000)}

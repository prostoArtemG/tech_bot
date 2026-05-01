function getCart() {
  try {
    return JSON.parse(localStorage.getItem('cart') || '[]');
  } catch (e) {
    return [];
  }
}

function saveCart(cart) {
  localStorage.setItem('cart', JSON.stringify(cart));
  updateCartCount();
}

function updateCartCount() {
  const cart = getCart();
  const count = cart.reduce((s, item) => s + (item.qty || 0), 0);
  const el = document.getElementById('cartCount');
  if (el) el.textContent = count;
}

function addToCart(id, name, price) {
  const cart = getCart();
  const idx = cart.findIndex(i => String(i.product_id) === String(id));
  if (idx >= 0) {
    cart[idx].qty = (cart[idx].qty || 1) + 1;
  } else {
    cart.push({ product_id: parseInt(id), name: name, price: parseFloat(price) || 0, qty: 1 });
  }
  saveCart(cart);
  alert('Добавлено в корзину');
}

function removeFromCart(id) {
  let cart = getCart();
  cart = cart.filter(i => String(i.product_id) !== String(id));
  saveCart(cart);
  renderCart();
}

function changeQty(id, qty) {
  const cart = getCart();
  const idx = cart.findIndex(i => String(i.product_id) === String(id));
  if (idx >= 0) {
    cart[idx].qty = Math.max(1, parseInt(qty) || 1);
    saveCart(cart);
    renderCart();
  }
}

function renderCart() {
  const cart = getCart();
  const list = document.getElementById('cartItems');
  const totalEl = document.getElementById('cartTotal');
  if (!list) return;
  list.innerHTML = '';
  let total = 0;
  cart.forEach(item => {
    const row = document.createElement('div');
    row.className = 'cart-row';
    row.innerHTML = `
      <div class="cart-name">${item.name}</div>
      <div class="cart-qty"><input type="number" value="${item.qty}" min="1" onchange="changeQty('${item.product_id}', this.value)"></div>
      <div class="cart-price">${(item.price||0).toFixed(0)} грн</div>
      <div class="cart-remove"><button onclick="removeFromCart('${item.product_id}')">Удалить</button></div>
    `;
    list.appendChild(row);
    total += (item.price || 0) * (item.qty || 0);
  });
  if (totalEl) totalEl.textContent = `${total.toFixed(0)} грн`;
  updateCartCount();
}

async function submitCartOrder(form) {
  const name = form.name.value || '';
  const phone = form.phone.value || '';
  const city = form.city.value || '';
  const comment = form.comment.value || '';
  const cart = getCart();
  if (!cart.length) { alert('Корзина пуста'); return; }

  const items = cart.map(i => ({ product_id: i.product_id, qty: i.qty }));

  const payload = { name, phone, city, comment, items };

  const res = await fetch('/api/cart-order', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  });

  const data = await res.json();
  if (data.ok) {
    localStorage.removeItem('cart');
    updateCartCount();
    alert('Заказ отправлен. Спасибо!');
    window.location.href = '/';
  } else {
    alert('Ошибка отправки заказа');
  }
}

document.addEventListener('DOMContentLoaded', function(){ updateCartCount(); if(document.getElementById('cartItems')) renderCart(); });

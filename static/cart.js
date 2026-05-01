function getCart() {
  try {
    return JSON.parse(localStorage.getItem("cart") || "[]");
  } catch (e) {
    return [];
  }
}

function saveCart(cart) {
  localStorage.setItem("cart", JSON.stringify(cart));
  updateCartCount();
}

function showToast(text) {
  let toast = document.getElementById("toast");
  if (!toast) {
    toast = document.createElement("div");
    toast.id = "toast";
    toast.className = "toast";
    document.body.appendChild(toast);
  }
  toast.textContent = text;
  toast.classList.add("show");

  setTimeout(() => {
    toast.classList.remove("show");
  }, 1800);
}

function addToCart(id, name, price) {
  let cart = getCart();
  let item = cart.find(x => x.id == id);

  if (item) {
    item.qty += 1;
  } else {
    cart.push({ id: String(id), name: name, price: Number(price), qty: 1 });
  }

  saveCart(cart);
  showToast("Товар добавлен в корзину");
}

function updateCartCount() {
  const el = document.getElementById("cartCount");
  if (!el) return;

  const cart = getCart();
  const count = cart.reduce((sum, item) => sum + item.qty, 0);
  el.textContent = count;
}

function removeFromCart(id) {
  let cart = getCart();
  cart = cart.filter(x => String(x.id) !== String(id));
  saveCart(cart);
  renderCart();
}

function changeQty(id, delta) {
  let cart = getCart();
  const item = cart.find(x => x.id == id);
  if (!item) return;
  const d = parseInt(delta) || 0;
  item.qty = (item.qty || 0) + d;
  if (item.qty <= 0) {
    cart = cart.filter(x => String(x.id) !== String(id));
  }
  saveCart(cart);
  renderCart();
}

function renderCart() {
  const list = document.getElementById('cartItems');
  const totalEl = document.getElementById('cartTotal');
  if (!list) return;
  const cart = getCart();
  list.innerHTML = '';
  let total = 0;
  cart.forEach(item => {
    const row = document.createElement('div');
    row.className = 'cart-item-card';
    row.innerHTML = `
      <div class="cart-item-main">
        <div class="cart-item-name">${item.name}</div>
        <div class="cart-item-price">${(item.price||0).toFixed(0)} грн</div>
      </div>
      <div class="cart-item-controls">
        <div class="qty-controls">
          <button type="button" class="qty-btn" onclick="changeQty('${item.id}', -1)">−</button>
          <span class="qty">${item.qty}</span>
          <button type="button" class="qty-btn" onclick="changeQty('${item.id}', 1)">+</button>
        </div>
        <button type="button" class="remove-small" onclick="removeFromCart('${item.id}')">Удалить</button>
      </div>
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

  const items = cart.map(i => ({ product_id: i.id, qty: i.qty }));

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
    showToast('Заказ отправлен. Спасибо!');
    window.location.href = '/';
  } else {
    showToast('Ошибка отправки заказа');
  }
}

document.addEventListener('DOMContentLoaded', function(){ updateCartCount(); if(document.getElementById('cartItems')) renderCart(); });

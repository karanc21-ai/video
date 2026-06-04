# Rudradhan Video Call Booking App

Render start command:

```bash
gunicorn app:app
```

Build command:

```bash
pip install -r requirements.txt
```

Main customer URL format:

```text
/book?store=in&handle=PRODUCT_HANDLE&source=instagram_story&campaign=ready_to_ship_video_call
```

Product-page Shopify Liquid URL format:

```liquid
https://video-9tgp.onrender.com/book?store=in&handle={{ product.handle | url_encode }}&sku={{ product.selected_or_first_available_variant.sku | url_encode }}&source=product_page&campaign=ready_to_ship_video_call
```

Admin:

```text
/admin
/admin/products
```

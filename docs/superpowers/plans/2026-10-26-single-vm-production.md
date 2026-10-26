# Tek VM üretim uyarlaması

Kullanıcının ayrıntılı uygulama talebine göre mevcut manager içinde ilerlenir.
Upstream release dosyaları, digest/policy, drain ve onaylı restore sınırları korunur.

1. `tests/test_manager.py`: eski config varsayılanları, doğrulama, kaynak/upload
   render çıktısı için önce başarısız test; `dd_manager.py` ve örnek config'lerde
   isteğe bağlı ayarlar. Worker 2; üretim web 2×2; PostgreSQL 18 `postgres -c`.
2. Depolama kontrolleri: DockerRootDir, gerçek volume Mountpoint/ölçüleri,
   veri diski ve zorunlu ayrı yedek mount'u; aynı filesystem gereksinimlerini topla.
   Backup için mevcut veri ölçüsü, restore için eski nesli silmeden yeni nesil,
   açılmış veri boyutu ve WAL/index payı. Düşük alan/mount kaybı testleri.
3. Backup/restore timeout'larını ayır; toplam ingress kapanma → tekrar hazır
   süresini kalıcı raporla. Eski yedek config'ini normalize et; hedefin kaynak,
   disk ve timeout ayarlarını restore sırasında koru. Regresyon testleri.
4. README ve üretim/pilot rehberi: 64 GB ortak bütçe, veri diski kurulum/denetim,
   host/container/app upload zinciri, süreler, sıralı reimport, gerçek ölçüm planı.
   systemd deadline'ını işleri zorla kesmeyecek biçimde düzenle.
5. `make test static validate`, geçici systemd doğrulaması, `trail build/check`.
   Canlı kurulum, gerçek import ve 500.000 bulguluk yük testi yapılmaz.

İnceleme odağı: eski config/backup, mount düşmesi, ortak diskte çift rezerv,
restore açılmış veri büyüklüğü, hata halinde süren bakım kesintisi.

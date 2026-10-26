# Doğrulama kaydı — 2026-10-23

Bu kayıt depo içindeki otomasyonun doğrulamasıdır; canlı kurum dağıtımı veya
4.000 varlık / 500.000 birikmiş bulgu kapasite onayı değildir.

## Ortam ve geçen kontroller

- Linux amd64, Python 3.14, Docker Engine 29.9.2, Compose 5.6.1.
- `make test`: **34 birim testi geçti**; gerçek Docker testi varsayılan koşuda skip.
  Kilit, inert dry-run, başarısız pull/backup/kopya, migration/health hatası,
  çalışan initializer'a güvenli retry, aktif HTTP isteği, dolu kuyruk, restore
  onayı/checksum/path/config reddi, retention ve secret maskeleme kapsanır.
  Zaman aşımı testi gerçek bir alt süreç oluşturarak Docker istemci grubunun
  deadline sonrasında işlem yapmaya devam edemediğini de denetler.
- `make static`: Python compileall geçti. Bütün Python dosyaları ayrıca Python
  3.10 dilbilgisi ile AST parse edildi; Python 3.10 üzerinde runtime koşusu yapılmadı.
- `make validate`: 3.4.201 ve 3.4.202 paket SHA-256'ları, tüm imaj digest'leri ve
  üretilmiş gerçek Compose konfigürasyonu `docker compose config --quiet` ile geçti.
- Dört systemd şablonu, kurmadan geçici kopyalarda repo yolu kullanılarak
  `systemd-analyze verify` ile geçti. `/opt/...` örnek yolu bu makinede kurulu
  olmadığı için değiştirilmemiş şablonun executable-path kontrolü beklenen hatayı verir.
- Bağımsız kod incelemesindeki network izolasyonu, çalışan initializer'ı retry'da
  kesme ve geç restore durum kaydı bulguları regresyon testleriyle giderildi.
- `trail check`, README'deki sabit özel `.runtime/compose.json` ile log okuma
  örneklerini upstream örnek Compose'u çalıştırmak diye yanlış işaretledi.
  README hiçbir yerde vendored ham `docker-compose.yml` ile deployment yapmaz;
  örnekler yalnızca manager'ın ürettiği özel sabit Compose üzerinden log okur.

## Gerçek Docker provası

`DDM_INTEGRATION=1 python3 -m unittest discover -s tests -p test_integration.py -v`
yalnızca rastgele `ddmtest...` projesi, loopback portu ve kendi volume'lerinde çalışır.
Test root'u/credentials geçici dizindedir, temizleme yalnız kendi proje etiketli
kaynakları hedefler. İndirilen sabit imajlar Docker cache'inde kalabilir.

İlk prova sırasında resmî registry'den ilk imaj indirmesi uzun sürdü; konteyner
oluşmadan prova kesilip imaj cache'i tamamlandıktan sonra tekrar başlatıldı.
Sonraki tam prova **927.249 sn** içinde geçti: gerçek 3.4.201 kurulumu, resmî
initializer, DB/media yedeği, örnek veriyi değiştirme, restore ve 3.4.202 upgrade.
DB satırı, media dosyası ve secret setinin korunduğu doğrulandı. Admin girişi ve
uWSGI istek denetimini de kapsayan sonraki tam koşu da **481.688 sn** içinde geçti.
Son subprocess grubu ve kurulum kimliği kontrolleriyle **3.4.202 sıfırdan smoke
testi de geçti** (13:56–13:58 UTC): initializer, frontend dahil bütün sağlık
kontrolleri, kalıcı proje kimliği ve özel uWSGI stats üzerinden güvenli boşaltma.
Gerçek GitHub `check-update` sorgusu da geçti; öncesi/sonrası bütün state dosyası
hash'leri aynı kaldı. Hiçbir uygulama upgrade'i bu sorgu tarafından uygulanmadı.

Son olarak PostgreSQL 18'in resmî parent-volume düzeniyle **tam yaşam döngüsü
427.226 sn içinde yeniden geçti** (14:02–14:09 UTC). Gerçek admin girişi,
quiesced dump/media yedeği, değiştirilmiş DB satırı ve dosyanın restore edilmesi,
secret setinin korunması ve 3.4.202 upgrade'i doğrulandı. Test sonunda konteyner
kalmadı; volume sayısı başlangıçtaki **19** değerine döndü. Önceki iki provanın
eski subdirectory mount düzeninden kalan iki boş anonim test volume'ü açık
kimlikleriyle temizlendi; mevcut kullanıcı volume'leri korunmuştur.

## Yükseltme yolları

Yalnızca **3.4.201 → 3.4.202** `releases/upgrade-paths.json` içinde açıldı:

| Kaynak | Hedef | Sonuç |
|---|---|---|
| 3.4.201 / `1b10e779eeeb624d75eb328f58fa8424ec09d000` | 3.4.202 / `8cbbe0d8c82d007662d270ccd7ab3365e2c84a83` | Üç gerçek izole backup/restore/upgrade provası geçti; sonuncusu nihai PostgreSQL 18 volume düzeniyle |

Bütün diğer kaynak/hedef çiftleri doğrulanmamıştır ve reddedilir. Bu prova kurum
verinizin özelliklerine veya büyük yükte upgrade süresine ilişkin garanti vermez.

## Bu teslimin doğrulamadığı alanlar

- Gerçek kurum Greenbone XML/CSV raporunun parser/import/reimport sonucu,
  bulgu kapanış kararları ve aylık 4.000 varlık / 500.000 birikmiş bulguda performans.
- Üretim verisiyle migration süresi, uzun çalışan gerçek import sırasında kesinti,
  elektrik/VM kaybı ve dolu diskten kurtarma provası.
- Kurum TLS/DNS/firewall, host proxy private key yedeği, uzak backup mount'u,
  notification hook ve timer'ların gerçek hosta kurulması/çalışması.
- ARM64 ve farklı PostgreSQL/Valkey ana sürüm geçişleri. Bu otomasyon bunları
  desteklenmiş gibi sessizce yürütmez.

İşletime almadan önce kurum verisinin kopyasıyla ayrı bir prova yapın. Yedekler
şifreli değildir; güvenli disk/uzak hedef operatörce sağlanmalıdır.


## Tek VM üretim uyarlaması — 2026-10-26

`make test static validate`: 53 test geçti, gerçek Docker yaşam döngüsü testi
skip edildi (toplam 54). Pilot/üretim profillerinin 3.4.201 ve 3.4.202 için dört
Compose çıktısı gerçek `docker compose config --quiet` ile doğrulandı. Yeni
regresyonlar eski config/yedek, upgrade/restore sırasında ayar korunması,
backup/restore komut deadline'ları, uzun drain/migration'da zorla öldürmeme,
ortak filesystem alan taleplerinin toplamı, ham veriye göre restore alanı,
mount kaybı/yanlış canlı volume, image-store symlink
yerleşimi, drain sırasında veri büyümesi, uzak hedef yokken yerel recovery,
kilidi alamayan komutun bakım raporunu değiştirmemesi, upload ayar uyuşmazlığı, nginx adapter'ının yeniden
başlatmada idempotent olması ve retry dahil toplam bakım süresini kapsar.
Nginx adapter'ı yerel gerçek shell/sed ile iki kez çalıştırıldı; bütünleşik yeni
container ayarları gerçek imajda bu teslimde başlatılmadı.

Bu değişiklikte host kurulumu, canlı dağıtım ve Docker lifecycle provası yapılmadı;
yukarıdaki 2026-10-23 sonuçları önceki kod sürümünün küçük örnekli kanıtıdır.
**500.000 bulguyla yük testi, gerçek büyük import/reimport, büyük backup/restore,
uzak mount kaybı ve 64 GB RAM kaynak ölçümü yapılmadı.** Uygulanabilir kabul adımları
[tek VM üretim rehberindedir](production.md). Config/disk testleri kontrollü
filesystem/Docker çıktıları kullanır; fiziksel 750 GB disk veya uzak NFS kanıtı değildir.

Geçici repo yolu uyarlamasıyla dört systemd şablonu `systemd-analyze verify`
ile doğrulandı; hosta unit kurulmadı. Bütün Python dosyaları Python 3.10
dilbilgisiyle AST parse edildi; 3.10 üzerinde runtime koşusu yapılmadı.


`trail check` çalıştırıldı; **exit 1, sözcük eşleşmesi uyarıları** verdi.
Bunlar kodla karşılaştırıldı: `backup_copy_mount` alanının varlığı recovery'yi
engellediği anlamına gelmez; `start` backup hedefine bakmaz, restore kullanılmayan
hedefi atlar. PostgreSQL parent target `/var/lib/postgresql` ve PGDATA
`/var/lib/postgresql/18/docker` değişmedi; network özel adları hâlâ kaldırılır.
Nginx env'i varsayılmıyor, container şablonu değiştirilir. Validate/test yalnız
üretilen özel Compose'u doğrular; ham upstream örnek dosyasıyla deployment ve
eski imajla şema rollback'i eklenmedi. `git diff -- releases` boş ve dört bundle
checksum doğrulaması geçti. Bu nedenle uyarılar semantik çatışma olarak kabul
edilmedi; önceki “Compose log inspection is not upstream deployment” öğrenimindeki
aynı sınırlama geçerlidir. Üretim/backup/recovery kararları yeni takım öğreniminde
kaydedildi. Bu kontroller host kurulumu veya canlı dağıtım yapmaz.

Containerd snapshotter kullanan prova hostunda gerçek image-store yolunu
`DDM_IMAGE_STORAGE_DIR=/verified/containerd/path make integration` ile bildirin.
Küçük lifecycle fixture'ının alan eşikleri 1 GiB'ye indirilmiştir; üretim
backup/restore alan ihtiyacını temsil etmez. Bu komut bu teslimde çalıştırılmadı.

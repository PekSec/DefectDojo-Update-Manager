# Sürüm paketi ve yükseltme politikası

İlk kurulum varsayılanı **3.4.202**. `releases/3.4.201` yalnızca önceki sürümden
geçişi test etmek ve kurtarma provaları için tutulur. İki sürümde de gerçek servis
seti `nginx`, `uwsgi`, `initializer`, `celeryworker`, `celerybeat`, `postgres`,
`valkey`; PostgreSQL ve Valkey imajları aynıdır. Kaynak kodu fork edilmez.

Her dizin şunları içerir:

- `docker-compose.yml`: commit kimliğiyle alınmış, değiştirilmemiş upstream dosya.
  İçindeki örnek parolalar ve `latest` varsayılanları **kullanılmaz**; bu dosyayı
  doğrudan çalıştırmayın.
- `LICENSE.md`: upstream BSD lisansı ve atıflar.
- `compose.json`: upstream Compose'un normalize edilmiş dağıtım tanımı; build,
  kaynak bind mount, örnek environment ve açık portlar çıkarılmıştır.
- `release.json`: commit, dosya SHA-256'ları, kaynak/release bağlantıları ve tüm
  servislerin tag + registry manifest digest'leri.

CLI, `compose.json` dosyasını ve checksum'ları denetler; ortamı `.runtime` içindeki
özel dosyadan ekler. PostgreSQL/Valkey dahil bütün imajlar digest ile sabittir.
Docker Hub resmî `defectdojo` imajları kullanılır; upstream kaynak derlenmez.
PostgreSQL 18 için ortam adaptörü named volume'ü `/var/lib/postgresql` üst dizinine
bağlar; `PGDATA=/var/lib/postgresql/18/docker` kullanır. Bu, imajın ilan ettiği
VOLUME ile eşleşir ve restore sonrası sahipsiz anonim parent volume bırakmaz.
Yedek/restore mantıksal `pg_dump`/`pg_restore` kullanır; eski raw veri dizinleri
bu araca doğrudan bağlanmamalıdır. [Resmî PostgreSQL imajı](https://hub.docker.com/_/postgres).

İncelenen resmî kaynaklar (2026-10-23):

- [Open Source kurulum](https://docs.defectdojo.com/get_started/open_source/installation/)
- [Sabit sürümün Docker rehberi](https://github.com/DefectDojo/django-DefectDojo/blob/8cbbe0d8c82d007662d270ccd7ab3365e2c84a83/readme-docs/DOCKER.md)
- [Open Source konfigürasyon](https://docs.defectdojo.com/get_started/open_source/configuration/)
- [Open Source üretim ve dosya yedeği](https://docs.defectdojo.com/get_started/open_source/running-in-production/)
- [Open Source upgrade rehberi](https://docs.defectdojo.com/releases/os_upgrading/upgrading_guide/)
- [3.4 genel geçiş notu](https://docs.defectdojo.com/releases/os_upgrading/3.4/)
- [3.4.200 geçiş notu](https://docs.defectdojo.com/releases/os_upgrading/3.4.200/)
- [3.4.201 release](https://github.com/DefectDojo/django-DefectDojo/releases/tag/3.4.201)
- [3.4.202 release](https://github.com/DefectDojo/django-DefectDojo/releases/tag/3.4.202)
- [PostgreSQL dump/restore](https://www.postgresql.org/docs/18/backup-dump.html)
- [uWSGI stats socket](https://uwsgi-docs.readthedocs.io/en/latest/StatsServer.html)
  ve [sinyaller](https://uwsgi-docs.readthedocs.io/en/latest/Management.html):
  istemci kopsa bile aktif istekler sıfır olmadan web süreci durdurulmaz.

3.4 genel notu özel bir geçiş adımı belirtmiyor. 3.4.200 notundaki menü değişikliği
Pro'ya özgü. Bunlar daha eski bir kurulumdan doğrudan 3.4.202'ye atlama izni
değildir. Örneğin [2.39 notu](https://docs.defectdojo.com/releases/os_upgrading/2.39/)
PostgreSQL ana sürüm geçişi içerir; yalnızca imajı değiştirmek yeterli olmaz.

`releases/upgrade-paths.json` izin verilen **tam kaynak/hedef çiftlerini** tutar.
`tested`, iki commit kimliği, incelenen notlar ve prova kanıtı zorunludur.
Bu dosyaya satır eklemek, çalışan kurulumda migration yürütme yetkisi verir;
dosyayı operatör kodu gibi inceleyin. CLI farklı PostgreSQL veya Valkey imajını
aynı veri volume'üyle başlatmaya izin vermez. Her yeni veritabanı/broker geçişi
ayrı veri taşıma prosedürü ve prova gerektirir.

Yeni sürüm eklerken:

1. Hedef kararlı release'i ve aradaki **bütün** release/geçiş notlarını okuyun.
   Breaking changes, settings, parser/deduplication ve PostgreSQL değişikliklerini
   değerlendirin. Gerekli ara sürümleri tek tek paketleyin.
2. `python3 tools/vendor_release.py VERSION COMMIT` ile yalnızca dağıtım
   dosyalarını ve digest'leri alın. Bu komut canlı servise dokunmaz; internet ve
   Compose CLI ister. İmajların servis mimarisini dosya diff'iyle inceleyin.
3. `make validate test static` çalıştırın. Yeni servis/volume/mount mimarisi varsa
   manager kodunu ve backup/drain/health kontrollerini birlikte güncelleyin.
4. Canlı yedeğin bir kopyasını **ayrı proje, port ve volume'lerde** restore edin.
   Yeni kaynak/hedef çifti için entegrasyon testini uyarlayın; sadece bu izole
   kopyada geçici policy ile migration, giriş, gerçek Greenbone import/reimport,
   worker görevleri ve restore provasını tamamlayın.
5. Kanıtı `docs/validation.md` benzeri bir dosyaya yazıp yalnızca test edilen
   kaynak/hedef çiftini policy'ye ekleyin. Canlı komut: `./dojo upgrade VERSION`.

Tag değişse bile digest değişmez; güncelleme kontrolü yeni imajı otomatik çekmez
ve policy oluşturmaz. Başarısız yükseltme sonrası eski imajla `start` yapmak şemayı
geri almaz. `config.json` içindeki `version` alanını elle değiştirmeyin.

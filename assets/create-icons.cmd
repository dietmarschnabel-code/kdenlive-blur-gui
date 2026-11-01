for size in 16 24 32 48 64 128 256 512; do
  magick kdenlive-blur-gui-1024.png \
    -fuzz 8% -fill none -draw "color 0,0 floodfill" \
    -strip \
    -define png:color-type=6 \
    -resize ${size}x${size} \
    icons/${size}.png
done
magick convert icons/256.png -define icon:auto-resize=256,64,32,16 kdenlive-blur-gui.ico

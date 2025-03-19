import React from "react";

export function ZdjeciaFirmy() {
  const photos = [
    { src: "/images/budynek1.jpg", alt: "Zdjęcie 1" },
    { src: "/images/budynek1.jpg", alt: "Zdjęcie 2" },
    { src: "/images/budynek1.jpg", alt: "Zdjęcie 3" },
    { src: "/images/budynek1.jpg", alt: "Zdjęcie 4" },
    { src: "/images/budynek1.jpg", alt: "Zdjęcie 5" },
    { src: "/images/budynek1.jpg", alt: "Zdjęcie 6" },
    { src: "/images/budynek1.jpg", alt: "Zdjęcie 7" },
    { src: "/images/budynek1.jpg", alt: "Zdjęcie 8" },
    { src: "/images/budynek1.jpg", alt: "Zdjęcie 9" },
    { src: "/images/budynek1.jpg", alt: "Zdjęcie 10" },
    { src: "/images/budynek1.jpg", alt: "Zdjęcie 11" },
    { src: "/images/budynek1.jpg", alt: "Zdjęcie 12" },
    { src: "/images/budynek1.jpg", alt: "Zdjęcie 13" },
    { src: "/images/budynek1.jpg", alt: "Zdjęcie 14" },
    { src: "/images/budynek1.jpg", alt: "Zdjęcie 15" },
    { src: "/images/budynek1.jpg", alt: "Zdjęcie 16" },


  ];

  return (
    <div>
      
      <h1 className="text-center mb-4">Zdjęcia Budynków Firmy</h1>
      <div className="photo-grid">
        {photos.map((photo, index) => (
          <div key={index} className="photo-item">
            <img
              src={photo.src}
              alt={photo.alt}
              className="w-full h-auto object-cover"
            />
          </div>
        ))}
      </div>
    </div>
  );
}
